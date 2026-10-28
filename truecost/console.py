"""The web console: one page over the same ledger(s) the in-agent face reads.

  truecost console --ledger ~/.truecost/ledger.db --ledger ~/.truecost/results.db --port 8686

Pages: Overview (the "usage page"), Live, Tasks + receipts, Crash-test ratings, Risk dial, Attacks.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import pricing
from .ledger import FAILED, UNVERIFIED, VERIFIED, Ledger
from .ratings import FAMILIES, MIN_RUNS, PROTOCOL_VERSION, THRESHOLDS, rate
from .report import AGENT_SIDE, GRID_G_CO2_PER_WH, compare_pooled, consistency, noise_floor, run_metrics

WEB = Path(__file__).parent / "web"
RANGES = {"today": None, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "all": None}
SEED_RE = re.compile(r"-s(\d+)$")


def _since(rng: str) -> float:
    if rng == "today":
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    sec = RANGES.get(rng)
    return time.time() - sec if sec else 0.0


class Console:
    def __init__(self, dbs: list[str]):
        self.ledgers = [Ledger(p) for p in dbs]
        self.replay: dict[str, Any] | None = None
        self._memo: dict[tuple, tuple[tuple, Any]] = {}
        self._memo_lock = threading.Lock()

    def _version(self) -> tuple:
        """Changes whenever any ledger file is written (WAL writes touch the -wal file)."""
        out = []
        for led in self.ledgers:
            for p in (led.path, led.path.with_name(led.path.name + "-wal")):
                st = p.stat() if p.exists() else None
                out.append((st.st_mtime_ns, st.st_size) if st else None)
        return tuple(out)

    def cached(self, key: tuple, compute) -> Any:
        """The board's bootstrap statistics take seconds; reuse them until a ledger changes."""
        with self._memo_lock:  # a request for a board being warmed waits for it instead of computing it twice
            version = self._version()
            hit = self._memo.get(key)
            if hit and hit[0] == version:
                return hit[1]
            value = compute()
            self._memo[key] = (version, value)
            return value

    def warm(self) -> None:
        """Compute every board the web pages ask for, so the first visit doesn't wait for the statistics."""
        for w in self.workloads():
            if "baseline" not in w["conditions"]:
                continue
            wl = w["workload"]
            self.cached(("crashtest", wl, None, None), lambda: self.crashtest(wl))
            self.cached(("crashtest", wl, None, "gov"), lambda: self.crashtest(wl, None, "gov"))
            self.cached(("attacks", wl), lambda: self.attacks(wl))

    def q(self, sql: str, args: tuple | list = ()) -> list[dict[str, Any]]:
        out = []
        for led in self.ledgers:
            out += led.query(sql, args)
        return out

    def ledger_for_run(self, run_id: str) -> Ledger | None:
        for led in self.ledgers:
            if led.one("SELECT 1 FROM runs WHERE run_id=?", (run_id,)) or \
               led.one("SELECT 1 FROM tasks WHERE run_id=? LIMIT 1", (run_id,)):
                return led
        return None

    # ---- overview -------------------------------------------------------------------------------
    def _scope(self, source: str) -> tuple[str, list]:
        """Usage views exclude crash-test experiments (runs with a condition) unless asked for them."""
        exp = "t.run_id IN (SELECT run_id FROM runs WHERE condition IS NOT NULL)"
        if source == "everything":
            return "", []
        if source == "crashtest":
            return f" AND {exp}", []
        sql = f" AND NOT {exp}"
        if source != "all":
            return sql + " AND t.source=?", [source]
        return sql, []

    def overview(self, rng: str, source: str) -> dict[str, Any]:
        since = _since(rng)
        src_sql, extra = self._scope(source)
        args = [since, *extra]
        marks = ",".join("?" for _ in AGENT_SIDE)
        rows = self.q(f"""SELECT t.task_key, t.source, t.outcome, COALESCE(t.started, MIN(c.ts_start)) started, t.violations,
                                 SUM(COALESCE(c.cost_usd,0)) usd, SUM(COALESCE(c.energy_j,0)) j,
                                 SUM(COALESCE(c.input_tokens,0)+COALESCE(c.cache_read_tokens,0)
                                     +COALESCE(c.cache_write_tokens,0)+COALESCE(c.output_tokens,0)) tok,
                                 COUNT(c.call_id) calls
                          FROM tasks t LEFT JOIN calls c ON c.task_key=t.task_key AND (c.role IS NULL OR c.role IN ({marks},'subagent'))
                          WHERE COALESCE(t.started, 0) >= ?{src_sql} GROUP BY t.task_key""",
                      [*AGENT_SIDE, *args])
        n = len(rows)
        usd = sum(r["usd"] for r in rows)
        j = sum(r["j"] for r in rows)
        ver = [r for r in rows if r["outcome"] == VERIFIED]
        failed = [r for r in rows if r["outcome"] == FAILED]
        unv = [r for r in rows if r["outcome"] == UNVERIFIED]
        days: dict[str, dict[str, float]] = defaultdict(lambda: {"verified": 0.0, "failed": 0.0, "unverified": 0.0})
        for r in rows:
            if not r["started"]:
                continue  # an imported task with no time on it or its calls: counted above, not placed on a day
            d = datetime.fromtimestamp(r["started"]).strftime("%Y-%m-%d")
            days[d][r["outcome"] if r["outcome"] in (VERIFIED, FAILED, UNVERIFIED) else "unverified"] += r["usd"]
        by_source: dict[str, dict[str, float]] = defaultdict(lambda: {"tasks": 0, "verified": 0, "usd": 0.0})
        for r in rows:
            b = by_source[r["source"]]
            b["tasks"] += 1
            b["verified"] += r["outcome"] == VERIFIED
            b["usd"] += r["usd"]
        measured = [r for r in ver if r["j"]]
        return {
            "range": rng, "source": source, "tasks": n, "verified": len(ver), "failed": len(failed),
            "unverified": len(unv), "usd": usd, "tokens": sum(r["tok"] for r in rows),
            "usd_per_verified": usd / len(ver) if ver else None,
            "usd_per_task": usd / n if n else None,
            "wasted_usd": sum(r["usd"] for r in failed), "unverified_usd": sum(r["usd"] for r in unv),
            "wasted_share": sum(r["usd"] for r in failed) / usd if usd else 0.0,
            "energy_wh": j / 3600.0 if j else None,
            "wh_per_verified": (j / 3600.0) / len(ver) if (j and ver) else None,
            "co2_g_per_verified": (j / 3600.0) * GRID_G_CO2_PER_WH / len(ver) if (j and ver) else None,
            "measured_tasks": len(measured),
            "violations": sum(r["violations"] or 0 for r in rows),
            "days": [{"day": d, **v} for d, v in sorted(days.items())][-30:],
            "by_source": by_source, "limits": self.limits(), "prices": pricing.snapshot_date(),
            "co2_factor": GRID_G_CO2_PER_WH,
        }

    def models(self, rng: str, source: str) -> list[dict[str, Any]]:
        """Every model the agents used: where it ran, its calls, the tasks it worked on and how they ended, tokens,
        list-price spend and measured energy (agent-side calls only, as everywhere else)."""
        src_sql, extra = self._scope(source)
        marks = ",".join("?" for _ in AGENT_SIDE)
        rows = self.q(f"""SELECT c.model, c.source,
                                 CASE WHEN c.tier LIKE 'sy%' AND c.gpu IS NULL THEN 'router'
                                      WHEN c.gpu IS NOT NULL OR c.energy_j IS NOT NULL THEN 'gpu' ELSE 'api' END kind,
                                 COUNT(*) calls, COUNT(DISTINCT c.task_key) tasks,
                                 COUNT(DISTINCT CASE WHEN t.outcome='verified' THEN c.task_key END) verified,
                                 COUNT(DISTINCT CASE WHEN t.outcome='failed' THEN c.task_key END) failed,
                                 SUM(COALESCE(c.input_tokens,0)+COALESCE(c.cache_read_tokens,0)
                                     +COALESCE(c.cache_write_tokens,0)+COALESCE(c.output_tokens,0)) tokens,
                                 SUM(c.cost_usd) usd, SUM(c.energy_j) j
                          FROM calls c JOIN tasks t ON t.task_key=c.task_key
                          WHERE (c.role IS NULL OR c.role IN ({marks},'subagent')) AND COALESCE(t.started,0) >= ?{src_sql}
                          GROUP BY 1, 2, 3""", [*AGENT_SIDE, _since(rng), *extra])
        where = {"gpu": "Self-hosted GPU", "api": "Hosted API", "router": "NVIDIA Switchyard route"}
        out = [{"model": r["model"], "source": r["source"],
                "where": {"claude_code": "Claude Code", "codex": "Codex"}.get(r["source"]) or where[r["kind"]],
                "calls": r["calls"], "tasks": r["tasks"], "verified": r["verified"], "failed": r["failed"],
                "tokens": r["tokens"], "usd": r["usd"], "energy_wh": r["j"] / 3600.0 if r["j"] else None}
               for r in rows if r["model"]]
        return sorted(out, key=lambda r: (r["where"], -(r["usd"] or 0)))

    def limits(self) -> list[dict[str, Any]]:
        latest: dict[tuple[str, str], dict] = {}
        for r in sorted(self.q("SELECT * FROM limits"), key=lambda r: r["ts"] or 0):  # newest across all ledgers
            latest[(r["source"], r["window"])] = r
        now = time.time()
        out = []
        for (s, w), r in sorted(latest.items()):
            if w in ("five_hour", "seven_day"):
                # a reading from a window that has since reset says nothing about now
                out.append({**r, "stale": bool(r["resets_at"] and r["resets_at"] < now)})
        return out

    # ---- tasks ----------------------------------------------------------------------------------
    def tasks(self, rng: str, source: str, run: str | None, limit: int) -> list[dict[str, Any]]:
        since = _since(rng)
        scope_sql, extra = self._scope(source)
        where, args = ["COALESCE(t.started,0) >= ?" + scope_sql], [since, *extra]
        if run:
            where.append("t.run_id=?")
            args.append(run)
        rows = self.q(f"""SELECT t.task_key, t.run_id, t.task_id, t.source, t.workload, t.title, t.started, t.ended,
                                 t.outcome, t.reward, t.partial, t.violations,
                                 SUM(COALESCE(c.cost_usd,0)) usd, SUM(COALESCE(c.energy_j,0)) j, COUNT(c.call_id) calls,
                                 SUM(COALESCE(c.input_tokens,0)+COALESCE(c.cache_read_tokens,0)
                                     +COALESCE(c.cache_write_tokens,0)+COALESCE(c.output_tokens,0)) tok
                          FROM tasks t LEFT JOIN calls c ON c.task_key=t.task_key AND (c.role IS NULL OR c.role NOT IN ('simulator','judge'))
                          WHERE {' AND '.join(where)} GROUP BY t.task_key ORDER BY t.started DESC LIMIT ?""",
                      [*args, limit])
        rows.sort(key=lambda r: -(r["started"] or 0))
        return rows[:limit]

    def task(self, key: str) -> dict[str, Any]:
        for led in self.ledgers:
            t = led.one("SELECT * FROM tasks WHERE task_key=?", (key,))
            if t:
                calls = led.query("""SELECT call_id, ts_start, ts_end, role, tier, model, input_tokens, cache_read_tokens,
                                            cache_write_tokens, output_tokens, reasoning_tokens, n_tool_calls, tool_names,
                                            finish_reason, latency_ms, energy_j, cost_usd, saver, decision, meta, status
                                     FROM calls WHERE task_key=? ORDER BY ts_start""", (key,))
                for c in calls:
                    for f in ("tool_names", "saver", "decision", "meta"):
                        c[f] = json.loads(c[f]) if c[f] else None
                t["meta"] = json.loads(t["meta"]) if t["meta"] else {}
                return {"task": t, "calls": calls}
        raise HTTPException(404, "task not found")

    def start_replay(self, run: str, speed: float) -> dict[str, Any]:
        rows = self.q("SELECT MIN(ts_start) t0, MAX(ts_start) t1, COUNT(*) n FROM calls WHERE run_id=? AND ts_start IS NOT NULL", (run,))
        r = next((x for x in rows if x["n"]), None)
        if not r:
            raise HTTPException(404, "run has no timed calls")
        self.replay = {"run": run, "speed": max(0.1, speed), "start": time.time(), "t0": r["t0"], "t1": r["t1"]}
        return {**self.replay, "duration_s": (r["t1"] - r["t0"]) / self.replay["speed"]}

    def replay_calls(self, since: float) -> list[dict[str, Any]]:
        rp = self.replay
        now = time.time()
        horizon = rp["t0"] + (now - rp["start"]) * rp["speed"]
        if horizon > rp["t1"] + 5:
            self.replay = None
        rows = self.q("""SELECT c.call_id, c.ts_start, c.ts_end, c.source, c.run_id, c.task_key, c.role, c.tier, c.model,
                                c.input_tokens, c.cache_read_tokens, c.output_tokens, c.n_tool_calls, c.tool_names,
                                c.energy_j, c.cost_usd, c.decision, c.saver, c.latency_ms, t.title, t.task_id
                         FROM calls c LEFT JOIN tasks t ON t.task_key=c.task_key
                         WHERE c.run_id=? AND c.ts_start <= ? ORDER BY c.ts_start DESC LIMIT 300""", (rp["run"], horizon))
        out = []
        for r in rows:
            r["ts_start"] = rp["start"] + (r["ts_start"] - rp["t0"]) / rp["speed"]  # re-timed onto the wall clock
            if r["ts_start"] > since:
                for f in ("tool_names", "decision", "saver"):
                    r[f] = json.loads(r[f]) if r[f] else None
                r["replay"] = True
                out.append(r)
        return out

    def live(self, since: float) -> list[dict[str, Any]]:
        if self.replay:
            return self.replay_calls(since)
        rows = self.q("""SELECT c.call_id, c.ts_start, c.ts_end, c.source, c.run_id, c.task_key, c.role, c.tier, c.model,
                                c.input_tokens, c.cache_read_tokens, c.output_tokens, c.n_tool_calls, c.tool_names,
                                c.energy_j, c.cost_usd, c.decision, c.saver, c.latency_ms, t.title, t.task_id
                         FROM calls c LEFT JOIN tasks t ON t.task_key=c.task_key
                         WHERE c.ts_start > ? ORDER BY c.ts_start DESC LIMIT 300""", (since,))
        for r in rows:
            for f in ("tool_names", "decision", "saver"):
                r[f] = json.loads(r[f]) if r[f] else None
        rows.sort(key=lambda r: -(r["ts_start"] or 0))
        return rows[:300]

    # ---- crash-test -----------------------------------------------------------------------------
    def workloads(self) -> list[dict[str, Any]]:
        """Workloads with at least one graded run: those with a baseline (so a crash-test board) first, then the richest."""
        rows = self.q("""SELECT r.workload, r.condition FROM runs r WHERE r.workload IS NOT NULL
                         AND EXISTS (SELECT 1 FROM tasks t WHERE t.run_id = r.run_id)""")
        out: dict[str, set] = defaultdict(set)
        for r in rows:
            out[r["workload"]].add(r["condition"])
        return sorted(({"workload": w, "conditions": sorted(c for c in cs if c)} for w, cs in out.items()),
                      key=lambda x: ("baseline" not in x["conditions"], -len(x["conditions"]), x["workload"]))

    def _by_cond(self, workload: str) -> dict[str, dict[str, str]]:
        """condition -> {seed: run_id}, graded runs only."""
        runs = self.q("SELECT run_id, condition FROM runs WHERE workload=?", (workload,))
        by_cond: dict[str, dict[str, str]] = defaultdict(dict)
        for r in runs:
            m = SEED_RE.search(r["run_id"])
            led = self.ledger_for_run(r["run_id"])
            if led is None or not led.one("SELECT 1 FROM tasks WHERE run_id=? LIMIT 1", (r["run_id"],)):
                continue  # not graded yet
            by_cond[r["condition"]][m.group(1) if m else "0"] = r["run_id"]
        return by_cond

    def batching(self, workload: str, gpu: int = 0) -> list[dict[str, Any]]:
        """Energy per call on one GPU vs how many calls were in flight (sampled over each call's lifetime)."""
        bins: dict[int, list[float]] = defaultdict(lambda: [0, 0.0, 0.0])
        for cond, seeds in self._by_cond(workload).items():
            for run in seeds.values():
                led = self.ledger_for_run(run)
                calls = led.query("""SELECT ts_start a, ts_end b, energy_j j FROM calls WHERE run_id=? AND gpu=?
                                     AND energy_j IS NOT NULL AND ts_end > ts_start ORDER BY ts_start""", (run, gpu))
                starts = [c["a"] for c in calls]
                import bisect
                for c in calls:
                    pts = [c["a"] + (c["b"] - c["a"]) * k / 9 for k in range(10)]
                    hi = bisect.bisect_right(starts, c["b"])
                    lo = max(0, hi - 64)
                    near = calls[lo:hi]
                    conc = sum(sum(1 for d in near if d["a"] <= p <= d["b"]) for p in pts) / 10
                    k = min(6, max(1, round(conc)))
                    bins[k][0] += 1
                    bins[k][1] += c["j"]
                    bins[k][2] += c["b"] - c["a"]
        return [{"in_flight": k, "calls": v[0], "j_per_call": v[1] / v[0], "watts_per_call": v[1] / v[2]}
                for k, v in sorted(bins.items()) if v[0]]

    def plans(self, window: str = "five_hour") -> dict[str, Any]:
        """Subscription runs: API-equivalent $ per pass vs % of the plan window per pass (see planmeter)."""
        from . import planmeter

        agents = []
        for led in self.ledgers:
            for source, label in (("codex", "Codex"), ("claude", "Claude Code")):
                if not led.one("SELECT 1 FROM tasks WHERE source=? AND meta LIKE '%limits_after%' LIMIT 1", (source,)):
                    continue
                f = planmeter.fit(led, source, window)
                runs = {r["run_id"]: r for r in led.query("SELECT run_id, condition, config FROM runs")}
                for r in f["runs"]:  # one row per configuration, seeds pooled
                    cfg = json.loads((runs.get(r["run_ids"][0]) or {}).get("config") or "{}")
                    r.update(effort=cfg.get("effort") or r.get("effort"),
                             tests="hidden" if "hidden" in (r["workload"] or "") else "visible")
                agents.append({"agent": label, **f})
        return {"window": window, "agents": agents}

    def attacks(self, workload: str) -> dict[str, Any]:
        """Attack cells are named "<condition>@<attack>"; amplification = attacked $/task over clean $/task."""
        by_cond = self._by_cond(workload)
        rows = []
        for cond, seeds in sorted(by_cond.items()):
            if "@" not in cond:
                continue
            clean, attack = cond.split("@", 1)
            if clean not in by_cond:
                continue
            pairs = [(by_cond[clean][s], seeds[s]) for s in seeds if s in by_cond[clean]]
            led = self.ledger_for_run(pairs[0][1]) if pairs else None
            if not led:
                continue
            cmp = compare_pooled(led, pairs, reps=100)
            rows.append({"condition": clean, "attack": attack, "amplification": cmp.get("usd_per_task_ratio"),
                         "tokens_ratio": cmp.get("tokens_per_task_ratio"), "success_delta_pp": cmp.get("success_delta_pp"),
                         "violations_delta_per_100": cmp.get("violations_delta_per_100")})
        return {"workload": workload, "rows": rows}

    def crashtest(self, workload: str, baseline: str | None = None, subset: str | None = None) -> dict[str, Any]:
        by_cond = self._by_cond(workload)
        task_ids = None
        if subset == "gov":  # restrict every comparison to the tasks the governor ran on (held-out split)
            task_ids = set()
            for cond, seeds in by_cond.items():
                if cond.startswith("gov") and "@" not in cond:
                    for run in seeds.values():
                        led = self.ledger_for_run(run)
                        task_ids |= {r["task_id"] for r in led.query("SELECT task_id FROM tasks WHERE run_id=?", (run,))}
            task_ids = task_ids or None
        base_name = baseline or next((c for c in ("baseline", "strong") if c in by_cond), None)
        if base_name is None:
            return {"workload": workload, "rows": [], "error": "No graded baseline run yet for this workload.",
                    "protocol": PROTOCOL_VERSION}
        base = by_cond[base_name]
        led = self.ledger_for_run(next(iter(base.values())))
        rows = []
        for cond, seeds in sorted(by_cond.items(), key=lambda kv: (kv[0] != base_name, kv[0])):
            if "@" in cond:
                continue  # attack cells are folded into their condition's security score below
            pairs = [(base[s], seeds[s]) for s in seeds if s in base]
            if not pairs:
                continue
            cmp = compare_pooled(led, pairs, reps=500, task_ids=task_ids)
            metrics = [run_metrics(led, r) for r in seeds.values()]
            amp = self._amplification(led, cond, seeds, by_cond)
            measures = {**cmp, "attack_amplification": amp}
            rows.append({"condition": cond, "seeds": sorted(seeds), "compare": cmp, "metrics": _mean_metrics(metrics),
                         "attack_amplification": amp, "stars": rate(measures),
                         "provisional": len(pairs) < MIN_RUNS,
                         "consistency": consistency(led, [seeds[k] for k in sorted(seeds)])})
        nf = noise_floor(led, [base[k] for k in sorted(base)]) if len(base) > 1 else None
        return {"workload": workload, "baseline": base_name, "rows": rows, "families": FAMILIES, "subset": subset,
                "noise_floor": nf,
                "subset_tasks": len(task_ids) if task_ids else None,
                "thresholds": THRESHOLDS, "protocol": PROTOCOL_VERSION}

    def _amplification(self, led: Ledger, cond: str, seeds: dict[str, str], by_cond) -> float | None:
        worst = None
        for c2, s2 in by_cond.items():
            if not c2.startswith(cond + "@"):
                continue
            pairs = [(seeds[s], s2[s]) for s in s2 if s in seeds]
            if pairs:
                r = compare_pooled(led, pairs, reps=50).get("usd_per_task_ratio")
                if r == r and r is not None:
                    worst = r if worst is None else max(worst, r)
        return worst


def _mean_metrics(ms: list[dict[str, Any]]) -> dict[str, Any]:
    keys = ["success_rate", "usd_per_verified", "usd_per_task", "tokens_per_task", "energy_wh_per_verified",
            "wasted_cost_share", "tail_cost_p95_over_median", "cheap_call_share", "violations_per_100", "partial_credit",
            "db_match_rate"]
    out = {"runs": len(ms), "tasks": sum(m.get("tasks", 0) for m in ms)}
    for k in keys:
        vals = [m[k] for m in ms if isinstance(m.get(k), (int, float))]
        out[k] = sum(vals) / len(vals) if vals else None
    return out


def _scrub(o):
    if isinstance(o, float):
        return None if (o != o or o in (float("inf"), float("-inf"))) else o
    if isinstance(o, dict):
        return {k: _scrub(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_scrub(v) for v in o]
    return o


class ScrubbedJSON(JSONResponse):
    """JSON with NaN/inf as null (ratios are undefined when a run has no verified task)."""

    def render(self, content) -> bytes:
        return super().render(_scrub(content))


def create_console(dbs: list[str], gates_dir: str | None = None, defaults: dict | None = None) -> FastAPI:
    """`defaults` seeds a first visit's filters (e.g. the demo shows every run, all time)."""
    con = Console(dbs)
    gates = Path(gates_dir).expanduser() if gates_dir else Path(dbs[0]).expanduser().parent / "gates"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        threading.Thread(target=con.warm, name="truecost-warm", daemon=True).start()
        yield

    app = FastAPI(title="TrueCost console", default_response_class=ScrubbedJSON, lifespan=lifespan)

    @app.get("/api/overview")
    def overview(range: str = "7d", source: str = "all"):
        return con.overview(range, source)

    @app.get("/api/models")
    def models(range: str = "7d", source: str = "all"):
        return con.models(range, source)

    @app.get("/api/defaults")
    def ui_defaults():
        return defaults or {}

    @app.get("/api/tasks")
    def tasks(range: str = "7d", source: str = "all", run: str | None = None, limit: int = 200):
        return con.tasks(range, source, run, min(limit, 2000))

    @app.get("/api/task")
    def task(key: str):
        return con.task(key)

    @app.get("/api/live")
    def live(since: float = 0.0):
        return {"now": time.time(), "calls": con.live(since or time.time() - 600)}

    @app.post("/api/replay")
    def replay(run: str, speed: float = 8.0):
        return con.start_replay(run, speed)

    @app.delete("/api/replay")
    def stop_replay():
        con.replay = None
        return {"ok": True}

    @app.get("/api/runs")
    def runs():
        return con.q("SELECT run_id, workload, condition, started FROM runs ORDER BY started DESC")

    @app.get("/api/workloads")
    def workloads():
        return con.workloads()

    @app.get("/api/crashtest")
    def crashtest(workload: str, baseline: str | None = None, subset: str | None = None):
        return con.cached(("crashtest", workload, baseline, subset), lambda: con.crashtest(workload, baseline, subset))

    @app.get("/api/batching")
    def batching(workload: str, gpu: int = 0):
        return con.batching(workload, gpu)

    @app.get("/api/plans")
    def plans(window: str = "five_hour"):
        return con.plans(window if window in ("five_hour", "seven_day") else "five_hour")

    @app.get("/api/attacks")
    def attacks(workload: str):
        return con.cached(("attacks", workload), lambda: con.attacks(workload))

    @app.get("/api/gate")
    def gate(name: str = "retail"):
        p = gates / f"{Path(name).name}.json"
        if not p.exists():
            return {"name": name, "missing": True}
        d = json.loads(p.read_text())
        return {"name": name, "data": d.get("data"), "report": d.get("report"), "ablation": d.get("ablation"),
                "features": d.get("features"), "updated": p.stat().st_mtime}

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app

