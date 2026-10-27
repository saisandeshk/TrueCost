"""How a subscription's usage meter charges each model: "% of a plan window per API-equivalent $", fitted.

Codex logs the account's live 5-hour and weekly usage on every turn; `claude -p` (stream-json) reports it once per
run. Between two consecutive readings in the same window the meter moved by Δ%, and the ledger knows which calls,
of which model, happened in between and their API-equivalent $. Non-negative least squares over all intervals:

    Δ%_i  ≈  Σ_model  k_model × $_model,i

Equal k across models means the plan meters by API price. A higher k means the model costs more of your plan
than its API price suggests. Readings are whole percents: consecutive intervals are summed into blocks, so the
rounding errors telescope (at most ±1 point per block), and a bootstrap over blocks (contiguous in time) gives
90% intervals. A model seen in too few blocks gets no estimate. Usage the ledger does not see (other sessions
on the same account) inflates every k alike.

Claude's reading arrives with its first response in a run, so it is timed at that run's first call (taken as
included in the reading). Readings without a known window (reset time) are not used.
"""

from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path
from typing import Any

from .ledger import Ledger

WINDOW_KEYS = {"five_hour": "primary", "seven_day": "secondary"}
CALL_SOURCE = {"claude": "claude_code"}  # subscription tasks are booked by agent name, their calls by collector


def _codex_readings(logs: list[Path], window: str) -> list[tuple[float, float, str, str]]:
    key = WINDOW_KEYS[window]
    out = set()
    for p in logs:
        try:
            lines = p.open(errors="replace").readlines()
        except OSError:
            continue
        for line in lines:
            if '"rate_limits"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            rl = ((d.get("payload") or {}).get("rate_limits") or {}).get(key) or {}
            if rl.get("used_percent") is None or not d.get("timestamp"):
                continue
            ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
            resets = rl.get("resets_at")
            if resets:  # resets_at jitters by a second between readings of one window: round to the minute
                out.add((ts, float(rl["used_percent"]), f"codex:{round(float(resets) / 60) * 60}", "codex"))
    return sorted(out)


def readings(led: Ledger, source: str, window: str) -> list[tuple[float, float, str, str]]:
    """(ts, used %, window id, account) from the agent logs of subscription runs. Runs on a shared account
    (whose meter also moved for usage the ledger does not see) contribute no readings."""
    if source == "codex":
        metas = [json.loads(t["meta"] or "{}") for t in
                 led.query("SELECT meta FROM tasks WHERE source=? AND meta LIKE '%limits_after%'", (source,))]
        if any(m.get("shared_account") for m in metas):
            metas = [m for m in metas if not m.get("shared_account")]
        return _codex_readings(sorted({Path(m["log"]) for m in metas if m.get("log")}), window)
    first = {r["task_key"]: (r["ts"], r["acct"]) for r in led.query(
        """SELECT task_key, MIN(ts_start) ts, MAX(json_extract(meta,'$.account')) acct FROM calls WHERE source=?
           GROUP BY task_key""", (CALL_SOURCE.get(source, source),))}
    keys = led.query("SELECT task_key, meta FROM tasks WHERE source=? AND meta LIKE '%limits_after%'", (source,))
    out = set()  # claude -p reports once per run, with its first response
    for t in keys:
        meta = json.loads(t["meta"] or "{}")
        if meta.get("shared_account"):
            continue  # the account's meter also moved for usage the ledger does not see
        lim = meta.get("limits_after") or {}
        ts, acct = first.get(t["task_key"], (None, None))
        acct = meta.get("account") or acct or ""
        if lim.get(window) is not None and lim.get(f"{window}_resets") and ts:
            out.add((float(ts), float(lim[window]), f"{acct}:{lim[f'{window}_resets']}", acct))
    return sorted(out)


def nnls(rows: list[list[float]], y: list[float], iters: int = 3000) -> list[float]:
    """Tiny non-negative least squares by projected coordinate descent (a handful of models)."""
    m = len(rows[0])
    k = [0.0] * m
    col_sq = [sum(r[j] ** 2 for r in rows) for j in range(m)]
    resid = list(y)
    for _ in range(iters):
        moved = 0.0
        for j in range(m):
            if col_sq[j] == 0:
                continue
            g = sum(r[j] * e for r, e in zip(rows, resid))
            new = max(0.0, k[j] + g / col_sq[j])
            d = new - k[j]
            if d:
                for i, r in enumerate(rows):
                    resid[i] -= d * r[j]
                k[j] = new
                moved = max(moved, abs(d))
        if moved < 1e-9:
            break
    return k


def _q(col: list[float], p: float) -> float | None:
    if not col:
        return None
    col = sorted(col)
    return col[min(len(col) - 1, max(0, int(p * len(col))))]


MIN_BLOCKS = 3  # a model needs spend in at least this many blocks to get an estimate
BLOCKS = (1, 2, 3, 5, 8, 10)
EXPLAINED = 0.9


TOKENS = "(COALESCE(input_tokens,0) + COALESCE(cache_read_tokens,0) + COALESCE(output_tokens,0)) / 1e6"


def fit(led: Ledger, source: str = "codex", window: str = "five_hour", basis: str = "auto",
        reps: int = 300, seed: int = 0, block: int | None = None) -> dict[str, Any]:
    """block=None picks the smallest block size whose fit explains >= 90% of the observed movement. Short
    intervals explain far less (Codex's readings lag its calls, smearing usage onto whichever model runs next),
    so per-interval fits are biased low; larger blocks trade that bias for fewer, coarser observations.

    basis="auto" fits both API $ and tokens and keeps the one that explains the meter with less pooling (then
    more of it). On Codex, tokens fit far better than $: the plan does not discount cache reads as the price
    list does."""
    if basis == "auto":
        cands = [fit(led, source, window, b, 0, seed, block) for b in ("usd", "tokens")]
        cands = [c for c in cands if c["delta_pct"]]
        if not cands:
            return fit(led, source, window, "usd", reps, seed, block)
        best = min(cands, key=lambda c: (c["block"], -c["explained_pct"] / c["delta_pct"]))
        out = fit(led, source, window, best["basis"], reps, seed, best["block"])
        out["alternatives"] = {c["basis"]: {"block": c["block"], "explained_pct": c["explained_pct"]} for c in cands}
        return out
    if block is None:
        valid = []  # fits with enough blocks to estimate anything
        for b in BLOCKS:
            f = _fit(led, source, window, basis, 0, seed, b)
            if not f["models"]:
                break  # larger blocks only leave fewer rows
            valid.append(f)
            if f["delta_pct"] and f["explained_pct"] >= EXPLAINED * f["delta_pct"]:
                break
        block = (max(valid, key=lambda f: f["explained_pct"] / (f["delta_pct"] or 1))["block"]
                 if valid and not (valid[-1]["explained_pct"] >= EXPLAINED * (valid[-1]["delta_pct"] or 1))
                 else valid[-1]["block"] if valid else BLOCKS[0])
    return _fit(led, source, window, basis, reps, seed, block)


def _fit(led: Ledger, source: str, window: str, basis: str, reps: int, seed: int, block: int) -> dict[str, Any]:
    amount = "cost_usd" if basis == "usd" else TOKENS
    calls = led.query(f"""SELECT c.ts_start ts, c.model, {amount.replace('cost_usd', 'c.cost_usd')} amt,
                                 COALESCE(json_extract(c.meta,'$.account'), ?) acct
                          FROM calls c LEFT JOIN tasks t ON t.task_key = c.task_key
                          WHERE c.source=? AND c.cost_usd IS NOT NULL AND json_extract(c.meta,'$.subscription')
                            AND NOT COALESCE(json_extract(t.meta,'$.shared_account'), 0)
                          ORDER BY c.ts_start""" if basis == "usd" else
                      f"""SELECT c.ts_start ts, c.model, {TOKENS.replace('input_tokens', 'c.input_tokens')
                                                         .replace('cache_read_tokens', 'c.cache_read_tokens')
                                                         .replace('output_tokens', 'c.output_tokens')} amt,
                                 COALESCE(json_extract(c.meta,'$.account'), ?) acct
                          FROM calls c LEFT JOIN tasks t ON t.task_key = c.task_key
                          WHERE c.source=? AND c.cost_usd IS NOT NULL AND json_extract(c.meta,'$.subscription')
                            AND NOT COALESCE(json_extract(t.meta,'$.shared_account'), 0)
                          ORDER BY c.ts_start""", ("codex" if source == "codex" else "", CALL_SOURCE.get(source, source)))
    reads = readings(led, source, window)
    models = sorted({c["model"] for c in calls})
    # intervals between consecutive readings of one account's meter, using only that account's calls, grouped into
    # runs of contiguous ones (same window). Concurrent sessions report the same meter, sometimes a stale value:
    # keep the running max within a window.
    segments: list[list[tuple[list[float], float]]] = []
    for acct in sorted({r[3] for r in reads}):
        mine = [c for c in calls if c["acct"] == acct]
        seq, top = [], {}
        for t, pct, wid, a in reads:
            if a == acct:
                top[wid] = max(top.get(wid, pct), pct)
                seq.append((t, top[wid], wid))
        segments.append([])
        ci = 0
        for (t0, p0, r0), (t1, p1, r1) in zip(seq, seq[1:]):
            while ci < len(mine) and mine[ci]["ts"] <= t0:
                ci += 1
            if r0 != r1:
                segments.append([])  # the window reset in between: rounding no longer telescopes across it
                continue
            x = [0.0] * len(models)
            j = ci
            while j < len(mine) and mine[j]["ts"] <= t1:
                x[models.index(mine[j]["model"])] += mine[j]["amt"] or 0.0
                j += 1
            segments[-1].append((x, p1 - p0))
    rows, y = [], []  # blocks: `block` consecutive intervals summed (zero-spend intervals included)
    for seg in segments:
        for i in range(0, len(seg), block):
            chunk = seg[i:i + block]
            x = [sum(c[0][j] for c in chunk) for j in range(len(models))]
            if any(x):
                rows.append(x)
                y.append(sum(c[1] for c in chunk))
    out: dict[str, Any] = {"source": source, "window": window, "basis": basis, "readings": len(reads),
                           "intervals": sum(len(sg) for sg in segments), "blocks": len(rows), "block": block,
                           "delta_pct": sum(y), "explained_pct": 0.0, "models": [], "runs": []}
    if len(rows) < 5 or not models:
        return out
    k = nnls(rows, y)
    out["explained_pct"] = sum(k[j] * r[j] for r in rows for j in range(len(models)))
    rng = random.Random(seed)
    boots = []
    for _ in range(reps):
        idx = [rng.randrange(len(rows)) for _ in rows]
        boots.append(nnls([rows[i] for i in idx], [y[i] for i in idx], iters=500))
    est: dict[str, tuple[float, list[float]] | None] = {}
    for j, m in enumerate(models):
        seen = sum(1 for r in rows if r[j] > 0)
        col = [b[j] for b in boots]
        ok = seen >= MIN_BLOCKS
        est[m] = (k[j], col) if ok else None
        out["models"].append({"model": m, "spent": sum(r[j] for r in rows), "blocks": seen,
                              "k": k[j] if ok else None, "lo": _q(col, 0.05) if ok else None,
                              "hi": _q(col, 0.95) if ok else None})
    # one row per (workload, condition): seeds pooled, so a configuration's plan cost uses all its passes
    groups: dict[tuple, dict[str, Any]] = {}
    for r in led.query("""SELECT t.run_id, t.workload, COALESCE(r.condition, t.run_id) cond,
                                 json_extract(t.meta,'$.effort') effort, COUNT(*) n, SUM(t.outcome='verified') v,
                                 (SELECT SUM(cost_usd) FROM calls c WHERE c.run_id=t.run_id) usd
                          FROM tasks t LEFT JOIN runs r ON r.run_id = t.run_id
                          WHERE t.source=? AND t.meta LIKE '%limits_after%'
                          GROUP BY t.run_id ORDER BY t.workload, t.run_id""", (source,)):
        g = groups.setdefault((r["workload"], r["cond"]), {"workload": r["workload"], "condition": r["cond"],
                                                           "effort": r["effort"], "run_ids": [], "n": 0, "v": 0,
                                                           "usd": 0.0, "spend": {}})
        g["run_ids"].append(r["run_id"])
        g["n"] += r["n"]
        g["v"] += r["v"] or 0
        g["usd"] = None if g["usd"] is None or r["usd"] is None else g["usd"] + r["usd"]
        for m in led.query(f"SELECT model, SUM({amount}) amt FROM calls WHERE run_id=? GROUP BY model", (r["run_id"],)):
            g["spend"][m["model"]] = g["spend"].get(m["model"], 0.0) + (m["amt"] or 0.0)
    for g in groups.values():
        spend = g.pop("spend")
        row = {**g, "run_id": ",".join(g["run_ids"]), "seeds": len(g["run_ids"]),
               "model": max(spend, key=spend.get) if spend else None,
               "usd_per_pass": g["usd"] / g["v"] if g["v"] and g["usd"] is not None else None,
               "plan_pct_per_pass": None, "lo": None, "hi": None}
        if g["v"] and spend and all(est.get(m) for m in spend):  # every model in it priced on the plan
            row["plan_pct_per_pass"] = sum(est[m][0] * a for m, a in spend.items()) / g["v"]
            col = [sum(est[m][1][b] * a for m, a in spend.items()) / g["v"] for b in range(len(boots))]
            row.update(lo=_q(col, 0.05), hi=_q(col, 0.95))
        out["runs"].append(row)
    return out
