"""Turn ledger rows into per-run metrics and paired comparisons (the crash-test numbers).

Cost and energy are counted on the agent side only: the agent's own calls plus any calls TrueCost
makes for it (summaries, governor). The simulated customer and the grader are test equipment and
are excluded, though their energy stays in the run-level meter reading.
"""

from __future__ import annotations

import json
import statistics
from typing import Any

from . import pricing
from .ledger import FAILED, VERIFIED, Ledger
from .safety import DOMAINS as POLICY_DOMAINS
from .safety import check as policy_check
from .stats import bootstrap_ci, cluster_bootstrap_ci, mcnemar_exact, percentile, sign_flip_p

AGENT_SIDE = ("agent", "summariser", "governor", "router")  # router: a routing judge's own model calls (Switchyard)
J_PER_WH = 3600.0
# Reported next to list-price $ and energy (they change no star; evaluation/PROTOCOL.md):
GPU_USD_PER_HOUR = 6.48    # NVIDIA Brev 1× H100 80 GB SXM (Nebius), as billed
GRID_G_CO2_PER_WH = 0.727  # India's grid, weighted average: CEA CO2 Baseline Database v20.0 (FY 2023-24), 0.727 t/MWh


def gpu_seconds(ledger: Ledger, run_id: str) -> dict[str, float]:
    """Each call's share of GPU time: while k calls are in flight on a GPU, each gets 1/k of that time (the same
    time-share split as energy). Every call on the GPU counts toward k, the simulated customer's too."""
    events: dict[int, list[tuple[float, int, str]]] = {}
    for r in ledger.query("SELECT call_id, ts_start, ts_end, gpu FROM calls WHERE run_id=? AND gpu IS NOT NULL "
                          "AND ts_end > ts_start", (run_id,)):
        ev = events.setdefault(r["gpu"], [])
        ev += [(r["ts_start"], 1, r["call_id"]), (r["ts_end"], -1, r["call_id"])]
    share: dict[str, float] = {}
    for ev in events.values():
        ev.sort(key=lambda e: (e[0], e[1]))  # at a tie, ends before starts
        live: set[str] = set()
        last = None
        for t, kind, cid in ev:
            if live and last is not None and t > last:
                dt = (t - last) / len(live)
                for c in live:
                    share[c] = share.get(c, 0.0) + dt
            last = t
            (live.add if kind == 1 else live.discard)(cid)
    return share


def import_tau2(ledger: Ledger, results_path: str, run_id: str, workload: str | None = None,
                domain: str | None = None) -> int:
    """Record τ² outcomes, with consequential policy violations from the transcript (truecost.safety)."""
    with open(results_path) as f:
        data = json.load(f)
    domain = domain or next((d for d in POLICY_DOMAINS if d in (workload or "")), "retail")
    n = 0
    for s in data["simulations"]:
        viol = policy_check(s.get("messages") or [], domain)
        tid, trial = str(s["task_id"]), s.get("trial") or 0
        key = f"{run_id}/{tid}" if not trial else f"{run_id}/{tid}#t{trial}"
        ri = s.get("reward_info") or {}
        r = ri.get("reward") or 0.0
        ledger.upsert_task({
            "task_key": key, "run_id": run_id, "task_id": tid, "source": "gateway", "workload": workload,
            "started": _when(s.get("start_time")), "ended": _when(s.get("end_time")),
            "outcome": VERIFIED if r >= 0.999 else FAILED, "reward": r,
            "violations": sum(v["consequential"] for v in viol),
            "meta": {"db_match": bool((ri.get("db_check") or {}).get("db_match")),
                     "policy": [{k: v[k] for k in ("rule", "step", "tool")} for v in viol if v["rule"] != "one_action"][:20],
                     "one_action": sum(v["rule"] == "one_action" for v in viol),
                     "termination": s.get("termination_reason"), "trial": trial,
                     "duration_s": s.get("duration"),
                     "n_messages": len(s.get("messages") or [])}})
        n += 1
    return n


def _when(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        from datetime import datetime
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _is_guardrail(atype: str) -> bool:
    """AutomationBench negative checks ("must not send", "row must not change", ...) are guardrails."""
    return "_not_" in atype or atype.endswith("_not")


def import_autobench(ledger: Ledger, export_path: str, run_id: str, workload: str | None = None) -> int:
    """Record AutomationBench outcomes: verified = every assertion passed (the official strict metric);
    partial = share of assertions passed; violations = failed guardrail assertions."""
    with open(export_path) as f:
        data = json.load(f)
    n = 0
    for t in data.get("tasks", []):
        results = [r for r in t.get("assertion_results") or [] if not r.get("excluded")]
        guards = [r for r in results if _is_guardrail(r.get("type", ""))]
        violated = [r["type"] for r in guards if not r.get("passed")]
        ledger.upsert_task({
            "task_key": f"{run_id}/{t['name']}", "run_id": run_id, "task_id": t["name"], "source": "gateway",
            "workload": workload, "outcome": VERIFIED if t.get("passed") else FAILED,
            "reward": 1.0 if t.get("passed") else 0.0, "partial": t.get("score"), "violations": len(violated),
            "meta": {"assertions": len(results), "guardrails": len(guards), "violated": violated[:10],
                     "steps": t.get("steps"), "model_calls": t.get("num_model_calls")}})
        n += 1
    return n


def per_task(ledger: Ledger, run_id: str) -> dict[str, dict[str, Any]]:
    """Graded tasks of a run with their agent-side tokens, cost and energy."""
    tasks = {t["task_key"]: {**t, "meta": json.loads(t["meta"]) if t["meta"] else {},
                             "tokens": 0, "input": 0, "cached": 0, "cache_write": 0, "output": 0, "cost": 0.0,
                             "energy_j": 0.0, "gpu_s": 0.0, "calls": 0, "strong_calls": 0, "cheap_calls": 0,
                             "unpriced_calls": 0, "unmetered_calls": 0}
             for t in ledger.query("SELECT * FROM tasks WHERE run_id=? AND outcome IN (?,?)",
                                   (run_id, VERIFIED, FAILED))}
    marks = ",".join("?" for _ in AGENT_SIDE)
    rows = ledger.query(
        f"""SELECT task_key, tier, COUNT(*) n, SUM(input_tokens) i, SUM(cache_read_tokens) c,
                   SUM(COALESCE(cache_write_tokens,0)) w, SUM(output_tokens) o,
                   SUM(COALESCE(cost_usd,0)) usd, SUM(COALESCE(energy_j,0)) j,
                   SUM(cost_usd IS NULL) unpriced, SUM(energy_j IS NULL) unmetered
            FROM calls WHERE run_id=? AND role IN ({marks}) GROUP BY task_key, tier""", (run_id, *AGENT_SIDE))
    for r in rows:
        t = tasks.get(r["task_key"])
        if t is None:
            continue
        t["input"] += r["i"] or 0
        t["cached"] += r["c"] or 0
        t["cache_write"] += r["w"] or 0
        t["output"] += r["o"] or 0
        t["tokens"] += (r["i"] or 0) + (r["c"] or 0) + (r["w"] or 0) + (r["o"] or 0)
        t["unpriced_calls"] += r["unpriced"] or 0
        t["unmetered_calls"] += r["unmetered"] or 0
        t["cost"] += r["usd"] or 0.0
        t["energy_j"] += r["j"] or 0.0
        t["calls"] += r["n"]
        if r["tier"] in ("strong", "cheap"):
            t[f"{r['tier']}_calls"] += r["n"]
    share = gpu_seconds(ledger, run_id)
    for r in ledger.query(f"SELECT call_id, task_key FROM calls WHERE run_id=? AND role IN ({marks})", (run_id, *AGENT_SIDE)):
        if r["task_key"] in tasks:
            tasks[r["task_key"]]["gpu_s"] += share.get(r["call_id"], 0.0)
    return tasks


def run_metrics(ledger: Ledger, run_id: str) -> dict[str, Any]:
    tasks = list(per_task(ledger, run_id).values())
    n = len(tasks)
    if n == 0:
        return {"run": run_id, "tasks": 0}
    ok = [t for t in tasks if t["outcome"] == VERIFIED]
    cost = sum(t["cost"] for t in tasks)
    energy = sum(t["energy_j"] for t in tasks)
    tokens = [t["tokens"] for t in tasks]
    calls = sum(t["calls"] for t in tasks)
    unmatched = ledger.one("SELECT COUNT(*) n FROM calls WHERE run_id=? AND task_key NOT IN "
                           "(SELECT task_key FROM tasks WHERE run_id=?)", (run_id, run_id))["n"]
    meter = ledger.query("SELECT * FROM energy WHERE run_id=?", (run_id,))
    costs = [t["cost"] for t in tasks]
    priced = all(t["unpriced_calls"] == 0 for t in tasks)
    metered = all(t["unmetered_calls"] == 0 for t in tasks) and energy > 0  # unmeasured is not zero
    med = statistics.median(costs) if costs else 0.0
    waste = ledger.one("""SELECT SUM(COALESCE(cost_usd,0)) usd, SUM(COALESCE(energy_j,0)) j FROM calls WHERE run_id=?
                          AND json_extract(decision, '$.action') IN ('draft-rejected', 'draft-pinned')""", (run_id,)) or {}
    acts = ledger.query("""SELECT json_extract(decision, '$.action') a, COUNT(*) n FROM calls WHERE run_id=? AND role='agent'
                           AND decision IS NOT NULL GROUP BY a""", (run_id,))
    return {
        "run": run_id, "tasks": n, "verified": len(ok), "success_rate": len(ok) / n,
        "db_match_rate": sum(1 for t in tasks if t["meta"].get("db_match")) / n,
        "partial_credit": statistics.mean(t["partial"] for t in tasks) if all(t["partial"] is not None for t in tasks) else None,
        "violations_per_100": 100 * sum(t["violations"] or 0 for t in tasks) / n
        if any(t["violations"] is not None for t in tasks) else None,
        "usd_total": cost if priced else None, "usd_per_task": cost / n if priced else None,
        "usd_per_verified": cost / max(len(ok), 1) if priced else None,
        "tokens_per_task": statistics.mean(tokens), "tokens_per_verified": sum(tokens) / max(len(ok), 1),
        "cache_share": sum(t["cached"] for t in tasks) / max(sum(t["input"] + t["cached"] + t["cache_write"] for t in tasks), 1),
        "energy_wh_per_task": energy / n / J_PER_WH if metered else None,
        "energy_wh_per_verified": energy / max(len(ok), 1) / J_PER_WH if metered else None,
        "gpu_usd_per_verified": sum(t["gpu_s"] for t in tasks) / 3600 * GPU_USD_PER_HOUR / max(len(ok), 1)
        if any(t["gpu_s"] for t in tasks) else None,  # no GPU timings (e.g. a vendor API): unknown, not zero
        "co2_g_per_verified": energy / max(len(ok), 1) / J_PER_WH * GRID_G_CO2_PER_WH if metered else None,
        "wasted_cost_share": sum(t["cost"] for t in tasks if t["outcome"] != VERIFIED) / cost if cost else 0.0,
        "wasted_energy_share": sum(t["energy_j"] for t in tasks if t["outcome"] != VERIFIED) / energy if metered else None,
        "tail_cost_p95_over_median": percentile(costs, 0.95) / med if med else float("nan"),
        "calls_per_task": calls / n,
        "cheap_call_share": sum(t["cheap_calls"] for t in tasks) / calls if calls else 0.0,
        "unmatched_calls": unmatched,
        "governor_overhead_cost_share": (waste.get("usd") or 0.0) / cost if cost else None,
        "governor_overhead_energy_share": (waste.get("j") or 0.0) / energy if energy else None,
        "governor_actions": {r["a"]: r["n"] for r in acts if r["a"]},
        "meter_j": {m["gpu"]: m["window_j"] for m in meter},
    }


def compare(ledger: Ledger, base_run: str, run: str, reps: int = 2000) -> dict[str, Any]:
    """Paired comparison on the tasks both runs graded (matched by task id and trial)."""
    return compare_pooled(ledger, [(base_run, run)], reps=reps)


def compare_pooled(ledger: Ledger, pairs: list[tuple[str, str]], reps: int = 2000,
                   task_ids: set[str] | None = None) -> dict[str, Any]:
    """Paired comparison pooled over several (baseline run, treatment run) pairs, e.g. one per seed.
    Tasks are matched by task id (and trial) within each pair."""
    a: dict[str, dict] = {}
    b: dict[str, dict] = {}
    for i, (base_run, run) in enumerate(pairs):
        for k, t in per_task(ledger, base_run).items():
            a[f"{i}|{k.split('/', 1)[1]}"] = t
        for k, t in per_task(ledger, run).items():
            b[f"{i}|{k.split('/', 1)[1]}"] = t
    ids = sorted(set(a) & set(b))
    if task_ids is not None:
        ids = [i for i in ids if i.split("|", 1)[1].split("#")[0] in task_ids]
    base = {"base": ",".join(p[0] for p in pairs), "run": ",".join(p[1] for p in pairs), "paired_tasks": len(ids)}
    if not ids:
        return base
    sa = [a[i]["outcome"] == VERIFIED for i in ids]
    sb = [b[i]["outcome"] == VERIFIED for i in ids]
    lost = sum(1 for x, y in zip(sa, sb) if x and not y)
    gained = sum(1 for x, y in zip(sa, sb) if y and not x)

    def ratio(idx, key):
        na = sum(sa[i] for i in idx)
        nb = sum(sb[i] for i in idx)
        base_v = sum(a[ids[i]][key] for i in idx) / na if na else 0.0
        if not base_v:
            return float("nan")  # undefined: no baseline success (or nothing measured)
        if not nb:
            return float("inf") if sum(b[ids[i]][key] for i in idx) > 0 else float("nan")  # spent, nothing verified
        return (sum(b[ids[i]][key] for i in idx) / nb) / base_v

    all_idx = list(range(len(ids)))
    by_task: dict[str, list[int]] = {}
    for n_, i in enumerate(ids):  # cluster = one task across all seeds and trials
        by_task.setdefault(i.split("|", 1)[1].split("#")[0], []).append(n_)
    clusters = list(by_task.values())
    diffs = [sum(int(sb[i]) - int(sa[i]) for i in c) / len(c) for c in clusters]
    out = {**base, "success_base": sum(sa) / len(ids), "success_run": sum(sb) / len(ids),
           "success_delta_pp": 100 * (sum(sb) - sum(sa)) / len(ids),
           "success_delta_ci95": cluster_bootstrap_ci(
               clusters, lambda idx: 100 * sum(sb[i] - sa[i] for i in idx) / len(idx), reps=reps),
           "tasks": len(clusters), "runs": len(pairs),
           "lost": lost, "gained": gained,
           # repeats of one task are not independent: McNemar only for a single run pair, otherwise a
           # task-cluster sign-flip test on per-task mean differences
           "mcnemar_p": mcnemar_exact(lost, gained) if all(len(c) == 1 for c in clusters) else None,
           # McNemar only when every task appears once; with repeats (seeds or trials) a task-cluster sign-flip test.
           # It assumes per-task differences are symmetric about 0 under the null and weights tasks equally.
           "p_value": mcnemar_exact(lost, gained) if all(len(c) == 1 for c in clusters) else sign_flip_p(diffs)}
    va = [a[i]["violations"] for i in ids]
    vb = [b[i]["violations"] for i in ids]
    if any(v is not None for v in va + vb):
        out["violations_base_per_100"] = 100 * sum(v or 0 for v in va) / len(ids)
        out["violations_run_per_100"] = 100 * sum(v or 0 for v in vb) / len(ids)
        out["violations_delta_per_100"] = out["violations_run_per_100"] - out["violations_base_per_100"]
    metered = all(a[i]["unmetered_calls"] == 0 and b[i]["unmetered_calls"] == 0 for i in ids)
    priced = all(a[i]["unpriced_calls"] == 0 and b[i]["unpriced_calls"] == 0 for i in ids)
    timed = any(a[i]["gpu_s"] for i in ids) and any(b[i]["gpu_s"] for i in ids)  # GPU time known on both sides
    for key, name in (("cost", "usd_per_verified"), ("energy_j", "energy_per_verified"), ("tokens", "tokens_per_verified"),
                      ("gpu_s", "gpu_usd_per_verified")):
        if (key == "energy_j" and not metered) or (key == "cost" and not priced) or (key == "gpu_s" and not timed):
            out[f"{name}_ratio"], out[f"{name}_ratio_ci95"] = None, (None, None)
            continue
        r = ratio(all_idx, key)
        lo, hi = cluster_bootstrap_ci(clusters, lambda idx, k=key: ratio(idx, k), reps=reps)
        out[f"{name}_ratio"] = r
        out[f"{name}_ratio_ci95"] = (lo, hi)
    for key, name in (("tokens", "tokens_per_task"), ("cost", "usd_per_task")):
        if key == "cost" and not priced:
            out[f"{name}_ratio"] = None
            continue
        x = sum(a[i][key] for i in ids) / len(ids)
        y = sum(b[i][key] for i in ids) / len(ids)
        out[f"{name}_ratio"] = y / x if x else float("nan")
    na, nb = sum(sa), sum(sb)
    out["usd_per_verified_base"] = sum(a[i]["cost"] for i in ids) / na if (na and priced) else None
    out["usd_per_verified_run"] = sum(b[i]["cost"] for i in ids) / nb if (nb and priced) else None
    out["wh_per_verified_base"] = sum(a[i]["energy_j"] for i in ids) / 3600 / na if (na and metered) else None
    out["wh_per_verified_run"] = sum(b[i]["energy_j"] for i in ids) / 3600 / nb if (nb and metered) else None
    out["gpu_usd_per_verified_base"] = sum(a[i]["gpu_s"] for i in ids) / 3600 * GPU_USD_PER_HOUR / na if (na and timed) else None
    out["gpu_usd_per_verified_run"] = sum(b[i]["gpu_s"] for i in ids) / 3600 * GPU_USD_PER_HOUR / nb if (nb and timed) else None
    out["co2_g_per_verified_base"] = out["wh_per_verified_base"] * GRID_G_CO2_PER_WH if out["wh_per_verified_base"] else None
    out["co2_g_per_verified_run"] = out["wh_per_verified_run"] * GRID_G_CO2_PER_WH if out["wh_per_verified_run"] else None
    calls_b = sum(b[i]["calls"] for i in ids)
    out["cheap_call_share_run"] = sum(b[i]["cheap_calls"] for i in ids) / calls_b if calls_b else None
    return out


def fmt_metrics(ms: list[dict[str, Any]]) -> str:
    keys = ["tasks", "verified", "success_rate", "db_match_rate", "partial_credit", "violations_per_100", "usd_per_verified", "usd_per_task", "tokens_per_task",
            "energy_wh_per_verified", "wasted_cost_share", "tail_cost_p95_over_median", "cheap_call_share", "unmatched_calls"]
    lines = ["| metric | " + " | ".join(m["run"] for m in ms) + " |", "|---|" + "---|" * len(ms)]
    for k in keys:
        cells = []
        for m in ms:
            v = m.get(k, "")
            if isinstance(v, float):
                v = f"{v:.4f}" if abs(v) < 10 else f"{v:,.0f}"
            cells.append(str(v))
        lines.append(f"| {k} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def backfill_tau2(ledger: Ledger, results_path: str, run_id: str, workload: str, condition: str, model: str,
                  tier: str = "strong", run_energy_j: float | None = None) -> int:
    """Import a τ² run that did not go through the gateway: tasks plus one call row per agent
    message, from τ²'s own per-message usage. Run-level energy, if given, is split across calls by
    generation time (an approximation, flagged in each row's meta)."""
    import_tau2(ledger, results_path, run_id, workload)
    ledger.upsert_run({"run_id": run_id, "workload": workload, "condition": condition,
                       "config": {"backfill": True, "model": model}})
    with open(results_path) as f:
        sims = json.load(f)["simulations"]
    rows = []
    for s in sims:
        tid, trial = str(s["task_id"]), s.get("trial") or 0
        key = f"{run_id}/{tid}" if not trial else f"{run_id}/{tid}#t{trial}"
        for i, m in enumerate(s.get("messages") or []):
            if m.get("role") != "assistant" or not m.get("usage"):
                continue
            u = m["usage"]
            prompt, out = u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0
            rows.append({"call_id": f"{key}#{i}", "source": "gateway", "run_id": run_id, "task_key": key,
                         "role": "agent", "tier": tier, "model": model, "api": "chat", "stream": 0,
                         "input_tokens": prompt, "cache_read_tokens": 0, "cache_write_tokens": 0,
                         "output_tokens": out, "reasoning_tokens": 0,
                         "n_tool_calls": len(m.get("tool_calls") or []),
                         "tool_names": [t.get("name") for t in m.get("tool_calls") or []],
                         "latency_ms": 1000.0 * (m.get("generation_time_seconds") or 0.0),
                         "cost_usd": pricing.cost_usd(model, prompt, 0, 0, out), "status": 200,
                         "meta": {"backfill": True}})
    if run_energy_j:
        total = sum(r["latency_ms"] for r in rows) or 1.0
        for r in rows:
            r["energy_j"] = run_energy_j * r["latency_ms"] / total
            r["meta"]["energy"] = "run-level, split by call duration"
    return ledger.add_calls(rows)


def import_polyglot(ledger: Ledger, results_path: str, run_id: str, workload: str | None = None) -> int:
    """Record Polyglot outcomes: verified = the exercise's original unit tests pass after the agent's edits."""
    with open(results_path) as f:
        data = json.load(f)
    for t in data["tasks"]:
        ledger.upsert_task({"task_key": f"{run_id}/{t['name']}", "run_id": run_id, "task_id": t["name"],
                            "source": "gateway", "workload": workload, "title": t["name"],
                            "outcome": VERIFIED if t["passed"] else FAILED, "reward": 1.0 if t["passed"] else 0.0,
                            "meta": {"agent": data.get("agent"), "agent_rc": t.get("agent_rc"),
                                     "duration_s": t.get("duration_s"), "test_tail": (t.get("test_tail") or "")[-200:]}})
    return len(data["tasks"])



def noise_floor(ledger: Ledger, baseline_runs: list[str]) -> dict[str, Any] | None:
    """How far apart two runs of the SAME condition land: |Δ success| (pp) and cost/energy ratios over all
    pairs of baseline runs. A trick's effect is only meaningful beyond this."""
    if len(baseline_runs) < 2:
        return None
    deltas, usd, wh = [], [], []
    for i in range(len(baseline_runs)):
        for j in range(i + 1, len(baseline_runs)):
            c = compare_pooled(ledger, [(baseline_runs[i], baseline_runs[j])], reps=200)
            if c.get("paired_tasks"):
                deltas.append(abs(c["success_delta_pp"]))
                usd.append(c.get("usd_per_verified_ratio"))
                wh.append(c.get("energy_per_verified_ratio"))
    if not deltas:
        return None
    dev = lambda xs: max(abs((x or 1.0) - 1.0) for x in xs)
    return {"pairs": len(deltas), "mean_abs_delta_pp": sum(deltas) / len(deltas), "max_abs_delta_pp": max(deltas),
            "max_usd_ratio_dev": dev(usd), "max_energy_ratio_dev": dev(wh)}


def consistency(ledger: Ledger, runs: list[str]) -> dict[str, Any] | None:
    """τ²-style pass^k over k runs of one condition: share of tasks solved in ALL runs (and in ANY run),
    on the tasks every run graded."""
    if len(runs) < 2:
        return None
    per = []
    for r in runs:
        per.append({k.split("/", 1)[1]: t["outcome"] == VERIFIED for k, t in per_task(ledger, r).items()})
    common = set.intersection(*(set(p) for p in per))
    if not common:
        return None
    return {"k": len(runs), "tasks": len(common),
            "pass_all": sum(all(p[t] for p in per) for t in common) / len(common),
            "pass_any": sum(any(p[t] for p in per) for t in common) / len(common),
            "flaky_share": sum(any(p[t] for p in per) and not all(p[t] for p in per) for t in common) / len(common)}
