"""The in-agent face: statusline, end-of-turn receipt line, /truecost receipt, and the breaker hook.

Works for Claude Code (statusline + hooks) and Codex (hooks; its status line only takes built-in items).
Both read the same ledger as the web console, refreshed from the agent's own session log on each call.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .collectors import claude_code, codex, is_test_command
from .ledger import FAILED, UNVERIFIED, VERIFIED, Ledger

DEFAULT_DB = "~/.truecost/ledger.db"
TOOL_GROUPS = {
    "Read": "reading", "Grep": "reading", "Glob": "reading", "WebFetch": "reading", "WebSearch": "reading",
    "Edit": "editing", "Write": "editing", "MultiEdit": "editing", "NotebookEdit": "editing", "apply_patch": "editing",
    "Bash": "running", "exec": "running", "exec_command": "running", "shell": "running",
    "Agent": "subagents", "Task": "subagents",
}
MARK = {VERIFIED: "✓", FAILED: "✗", UNVERIFIED: "?"}


def projects() -> list[str]:
    env = os.environ.get("TRUECOST_PROJECTS")
    if env:
        return [p for p in env.split(os.pathsep) if p]
    return [os.getcwd()]


def refresh(ledger: Ledger, agent: str, transcript: str | None = None, session_id: str | None = None,
            cwd: str | None = None) -> str | None:
    """Re-read this session's log into the ledger. Returns the session id."""
    # with TRUECOST_PROJECTS set (a global hook), only those projects are read; otherwise the session's own folder
    roots = projects() + ([cwd] if cwd and not os.environ.get("TRUECOST_PROJECTS") else [])
    if agent == "claude":
        if not transcript:
            return session_id
        p = Path(transcript).expanduser()
        config_dir = p.parents[2] if len(p.parents) > 2 else None
        account = f"claude_code:{config_dir.name}" if config_dir is not None else None
        calls, tasks = claude_code.parse_transcript(p, roots, account)
        sub = sorted((p.parent / p.stem).glob("**/*.jsonl")) if (p.parent / p.stem).exists() else []
        for f in sub:
            c2, t2 = claude_code.parse_transcript(f, roots, account)
            calls += c2
            tasks += t2
        claude_code._attach_orphans(calls, tasks)
        if calls:
            ledger.add_calls(calls)
        keep = {t["task_key"] for t in tasks}
        sessions = {t["task_key"].split(":")[1] for t in tasks} | {c["session_id"] for c in calls if c.get("session_id")}
        for sess in sessions:  # drop tasks whose numbering changed (or that vanished) on re-parse
            for r in ledger.query("SELECT task_key FROM tasks WHERE task_key LIKE ?", (f"cc:{sess}:%",)):
                if r["task_key"] not in keep:
                    ledger.conn.execute("DELETE FROM tasks WHERE task_key=?", (r["task_key"],))
        for t in tasks:
            ledger.upsert_task(t, merge=False)
        if config_dir is not None:
            for lim in claude_code.read_limits(config_dir):
                ledger.add_limit(f"claude_code:{config_dir.name}", lim["window"], lim["used_pct"],
                                 lim["resets_at"], lim["ts"])
        return session_id or p.stem
    if agent == "codex":
        files = []
        if transcript:
            files = [Path(transcript).expanduser()]
        elif session_id:
            files = sorted(Path("~/.codex/sessions").expanduser().glob(f"**/rollout-*{session_id}*.jsonl"))
        if files:
            codex.collect(ledger, projects=roots, files=files)
        return session_id
    return session_id


def _source(agent: str) -> str:
    return "claude_code" if agent == "claude" else "codex"


def task_cost(ledger: Ledger, task_key: str) -> dict[str, Any]:
    r = ledger.one("""SELECT COUNT(*) calls, SUM(COALESCE(cost_usd,0)) usd, SUM(input_tokens) i,
                             SUM(cache_read_tokens) cr, SUM(cache_write_tokens) cw, SUM(output_tokens) o
                      FROM calls WHERE task_key=?""", (task_key,)) or {}
    return {k: (v or 0) for k, v in r.items()}


def burn_rate(ledger: Ledger, account: str, window: str) -> float | None:
    """Estimated % of a subscription window per API-equivalent $ for an account, from consecutive limit readings
    within one window (same reset time): Δ% used / $ spent by the account's observed calls in between.
    Approximate: usage outside allow-listed projects is not observed, so it errs towards more % per $."""
    rows = ledger.query("SELECT ts, used_pct, resets_at FROM limits WHERE source=? AND window=? ORDER BY ts",
                        (account, window))
    d_pct = d_usd = 0.0
    for a, b in zip(rows, rows[1:]):
        if a["resets_at"] != b["resets_at"] or b["used_pct"] <= a["used_pct"]:
            continue
        usd = ledger.one("""SELECT SUM(COALESCE(cost_usd,0)) s FROM calls WHERE ts_start > ? AND ts_start <= ?
                            AND json_extract(meta, '$.account') = ?""", (a["ts"], b["ts"], account))["s"] or 0.0
        if usd > 0:
            d_pct += b["used_pct"] - a["used_pct"]
            d_usd += usd
    return d_pct / d_usd if d_usd >= 0.5 and d_pct >= 1 else None


def plan_delta(ledger: Ledger, source: str, window: str, since: float) -> float | None:
    """Measured % of a plan window used since `since`: latest reading minus the last reading before it, when both
    fall in the same window. Includes anything else the account did meanwhile (other sessions)."""
    before = ledger.one("""SELECT used_pct, resets_at FROM limits WHERE source=? AND window=? AND ts<=?
                           ORDER BY ts DESC LIMIT 1""", (source, window, since))
    now = ledger.one("SELECT used_pct, resets_at, ts FROM limits WHERE source=? AND window=? ORDER BY ts DESC LIMIT 1",
                     (source, window))
    if not before or not now or now["ts"] <= since or before["resets_at"] != now["resets_at"]:
        return None
    return max(0.0, now["used_pct"] - before["used_pct"])


def account_of(transcript: str | None) -> str | None:
    """The Claude Code account (config dir) a session belongs to, from its transcript path."""
    if not transcript:
        return None
    parents = Path(transcript).expanduser().parents
    return f"claude_code:{parents[2].name}" if len(parents) > 2 else None


def summary(ledger: Ledger, agent: str, session_id: str | None, account: str | None = None) -> dict[str, Any]:
    src = _source(agent)
    out: dict[str, Any] = {"agent": agent}
    if session_id:
        prefix = "cc" if agent == "claude" else "codex"
        cur = ledger.one(f"SELECT * FROM tasks WHERE source=? AND task_key LIKE ? ORDER BY started DESC LIMIT 1",
                         (src, f"{prefix}:{session_id}:%"))
        if cur:
            out["task"] = {**cur, **task_cost(ledger, cur["task_key"]),
                           "n": int(cur["task_key"].rsplit(":", 1)[1]) if agent == "claude" else None,
                           "meta": json.loads(cur["meta"] or "{}")}
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = ledger.query("""SELECT t.task_key, t.outcome, SUM(COALESCE(c.cost_usd,0)) usd FROM tasks t
                           LEFT JOIN calls c ON c.task_key = t.task_key
                           WHERE t.run_id='observer' AND t.started >= ? GROUP BY t.task_key""", (midnight,))
    tot = sum(r["usd"] for r in rows)
    ver = [r for r in rows if r["outcome"] == VERIFIED]
    out["today"] = {"tasks": len(rows), "verified": len(ver), "failed": sum(r["outcome"] == FAILED for r in rows),
                    "usd": tot, "usd_per_verified": tot / len(ver) if ver else None,
                    "wasted_share": sum(r["usd"] for r in rows if r["outcome"] == FAILED) / tot if tot else 0.0}
    lim_src = "codex" if agent == "codex" else account  # this session's own account, never another one's
    if agent == "claude" and not lim_src:
        latest = ledger.one("SELECT source FROM limits WHERE source LIKE 'claude_code:%' ORDER BY ts DESC LIMIT 1")
        lim_src = latest["source"] if latest else None
    if lim_src:
        lims = {}
        for w in ("five_hour", "seven_day"):
            r = ledger.one("SELECT used_pct, resets_at FROM limits WHERE source=? AND window=? ORDER BY ts DESC LIMIT 1",
                           (lim_src, w))
            if r:
                lims[w] = r
        out["limits"] = lims
        if out.get("task"):
            started = out["task"].get("started") or 0
            out["task"]["five_hour_pct"] = plan_delta(ledger, lim_src, "five_hour", started)
            rate = burn_rate(ledger, lim_src, "seven_day")
            if rate:
                out["task"]["week_pct"] = out["task"]["usd"] * rate
    return out


def _usd(x: float | None) -> str:
    if x is None:
        return "–"
    return f"${x:.2f}" if x >= 0.995 else f"${x:.3f}".rstrip("0")


def format_line(s: dict[str, Any], width: int = 0) -> str:
    parts = ["TrueCost"]
    t = s.get("task")
    if t:
        title = (t.get("title") or "")[:28]
        burn = ""
        if t.get("five_hour_pct") is not None:
            burn = f" · {t['five_hour_pct']:.0f}% of 5h"
        elif t.get("week_pct"):
            burn = f" · ≈{t['week_pct']:.1f}% of week"
        parts.append(f"▸ {title} · {_usd(t['usd'])}{burn} · tests {MARK.get(t['outcome'], '?')}")
    d = s.get("today") or {}
    if d.get("tasks"):
        parts.append(f"today {d['verified']}/{d['tasks']} ✓ · {_usd(d['usd_per_verified'])}/✓ · "
                     f"{100 * d['wasted_share']:.0f}% wasted")
    lims = s.get("limits") or {}
    if lims:
        bits = []
        if "five_hour" in lims:
            bits.append(f"5h {lims['five_hour']['used_pct']:.0f}%")
        if "seven_day" in lims:
            bits.append(f"wk {lims['seven_day']['used_pct']:.0f}%")
        parts.append(" · ".join(bits))
    return " │ ".join(parts)


def receipt(ledger: Ledger, task_key: str) -> str:
    t = ledger.one("SELECT * FROM tasks WHERE task_key=?", (task_key,))
    if not t:
        return "TrueCost: no task yet in this session."
    meta = json.loads(t["meta"] or "{}")
    c = task_cost(ledger, task_key)
    calls = ledger.query("SELECT tool_names, cost_usd, model FROM calls WHERE task_key=?", (task_key,))
    where: Counter = Counter()
    for r in calls:
        tools = json.loads(r["tool_names"] or "[]")
        groups = [TOOL_GROUPS.get(x, "other tools") for x in tools] or ["thinking & answers"]
        for g in groups:
            where[g] += (r["cost_usd"] or 0) / len(groups)
    tot = sum(where.values()) or 1.0
    models = Counter(r["model"] for r in calls)
    tests = meta.get("tests") or []
    lines = [f"Task: {t['title']}    {MARK.get(t['outcome'], '?')} {t['outcome']}"]
    if tests:
        last = tests[-1]
        when = datetime.fromtimestamp(last["ts"]).strftime("%H:%M") if last.get("ts") else ""
        lines[0] += f" (last test exit {last['exit']} at {when})"
    tok_in = c["i"] + c["cr"] + c["cw"]
    lines.append(f"  {c['calls']} model calls · {tok_in + c['o']:,} tokens "
                 f"({100 * c['cr'] / tok_in if tok_in else 0:.0f}% cache hits) · API-equivalent {_usd(c['usd'])}")
    lines.append("  models: " + ", ".join(f"{m} ×{n}" for m, n in models.most_common()))
    lines.append("  where it went: " + " · ".join(f"{g} {100 * v / tot:.0f}%" for g, v in where.most_common()))
    fails = Counter(x["cmd"] for x in tests if x["exit"] != 0)
    worst = fails.most_common(1)
    if worst and worst[0][1] >= 2:
        lines.append(f"  waste: the same failing test ran ×{worst[0][1]}: {worst[0][0][:60]}")
    return "\n".join(lines)


# ---- hooks --------------------------------------------------------------------------------------
BREAKER_REPEATS = int(os.environ.get("TRUECOST_BREAKER_REPEATS", "3"))
BREAKER_TASK_USD = float(os.environ.get("TRUECOST_TASK_BUDGET_USD", "0") or 0)


def allowed(cwd: str | None) -> bool:
    """A global hook (Codex) stays silent outside the allow-listed projects."""
    if not os.environ.get("TRUECOST_PROJECTS") or not cwd:
        return True
    c = os.path.realpath(cwd)
    return any(c == os.path.realpath(p) or c.startswith(os.path.realpath(p) + os.sep) for p in projects())


def hook(event: str, agent: str, payload: dict, ledger: Ledger) -> dict | None:
    if not allowed(payload.get("cwd")):
        return None
    sid = refresh(ledger, agent, payload.get("transcript_path"), payload.get("session_id"), payload.get("cwd"))
    s = summary(ledger, agent, sid, account_of(payload.get("transcript_path")))
    if event == "stop":
        return {"systemMessage": format_line(s)}
    if event == "pretooluse":
        cmd = ((payload.get("tool_input") or {}).get("command") or (payload.get("tool_input") or {}).get("cmd") or "")
        t = s.get("task")
        if not t:
            return None
        reasons = []
        if is_test_command(cmd):
            fails = [x for x in (t["meta"].get("tests") or []) if x["exit"] != 0 and x["cmd"].strip() == cmd.strip()]
            if len(fails) >= BREAKER_REPEATS:
                reasons.append(f"this exact test has already failed ×{len(fails)} in this task")
        if BREAKER_TASK_USD and t["usd"] > BREAKER_TASK_USD:
            reasons.append(f"this task has spent {_usd(t['usd'])} (budget {_usd(BREAKER_TASK_USD)})")
        if reasons:
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "ask",
                "permissionDecisionReason": "TrueCost breaker: " + "; ".join(reasons)
                + ". Change approach before spending more?"}}
    return None


def main_status(agent: str, stdin_text: str, db: str) -> str:
    payload = json.loads(stdin_text) if stdin_text.strip() else {}
    try:  # keep the last payload: shows which fields (e.g. live rate limits) the agent provides
        Path(DEFAULT_DB).expanduser().parent.joinpath(f"last_status_{agent}.json").write_text(json.dumps(payload)[:20000])
    except OSError:
        pass
    led = Ledger(db)
    sid = refresh(led, agent, payload.get("transcript_path"), payload.get("session_id"), payload.get("cwd")
                  or (payload.get("workspace") or {}).get("current_dir"))
    record_live_limits(led, agent, payload)
    return format_line(summary(led, agent, sid, account_of(payload.get("transcript_path"))))


def record_live_limits(ledger: Ledger, agent: str, payload: dict) -> int:
    """Claude Code passes live subscription usage to the statusline (rate_limits.five_hour/seven_day). Book a
    reading when it changes, or at most once a minute when it does not (an unchanged reading is still evidence
    that nothing was used since); this is fresher than the cached copy in .claude.json."""
    lims = payload.get("rate_limits") or {}
    tp = payload.get("transcript_path")
    if agent != "claude" or not lims or not tp:
        return 0
    parents = Path(tp).expanduser().parents
    if len(parents) <= 2:
        return 0
    source, n = f"claude_code:{parents[2].name}", 0
    for window, v in lims.items():
        if not isinstance(v, dict) or v.get("used_percentage") is None:
            continue
        pct, resets = float(v["used_percentage"]), v.get("resets_at")
        last = ledger.one("SELECT ts, used_pct, resets_at FROM limits WHERE source=? AND window=? ORDER BY ts DESC LIMIT 1",
                          (source, window))
        if last and last["used_pct"] == pct and last["resets_at"] == resets and time.time() - last["ts"] < 60:
            continue
        ledger.add_limit(source, window, pct, resets)
        n += 1
    return n
