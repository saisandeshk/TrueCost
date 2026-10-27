"""Claude Code collector: transcripts in <config dir>/projects/*/*.jsonl -> ledger calls and tasks.

A task is one user prompt and the agent's work until the next prompt. It is "verified" when the last
test command the agent ran for it passed, "failed" when that run failed, and "unverified" when no
tests ran. Claude Code writes one line per content block with the message's usage repeated on each,
so calls are de-duplicated by message id (summing lines over-counts several-fold).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import pricing
from ..ledger import FAILED, UNVERIFIED, VERIFIED, Ledger
from . import is_test_command, title_of, under

SOURCE = "claude_code"
_EXIT = re.compile(r"Exit code (\d+)")
_NOT_PROMPT = ("<local-command", "<command-name>", "<command-message>", "<bash-input>", "<bash-stdout>",
               "<bash-stderr>", "<task-notification>", "[Request interrupted", "<system-reminder>")


# user-role turns the harness writes (not prompts); "human", "sdk" (claude -p, SDK clients) and absent are prompts
HARNESS_ORIGINS = {"task_notification", "auto_continuation"}


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _prompt_text(msg: dict) -> str | None:
    """The user's own prompt text, or None for tool results, meta lines and command echoes."""
    c = msg.get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(c, str) or not c.strip():
        return None
    if c.lstrip().startswith(_NOT_PROMPT):
        return None
    return c


def parse_transcript(path: Path, projects: list[str], account: str | None = None) -> tuple[list[dict], list[dict]]:
    """Return (calls, tasks) for one transcript file; empty if its cwd is not allow-listed."""
    calls: dict[str, dict] = {}
    tasks: list[dict] = []
    tests: dict[str, list[tuple[float, int, str]]] = {}
    bash_cmds: dict[str, str] = {}
    current: dict[str, str] = {}  # session -> task_key
    allowed: bool | None = None
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            if t not in ("user", "assistant"):
                continue
            if allowed is None and d.get("cwd"):
                allowed = under(d["cwd"], projects)
                if not allowed:
                    return [], []
            sid = d.get("sessionId") or path.stem
            ts = _ts(d.get("timestamp"))
            msg = d.get("message") or {}
            if t == "user":
                # compaction summaries and harness-originated turns (task notifications) continue the current task
                harness = d.get("isCompactSummary") or d.get("turnOrigin") in HARNESS_ORIGINS
                text = None if d.get("isMeta") or d.get("isSidechain") or harness else _prompt_text(msg)
                if text is not None:
                    key = f"cc:{sid}:{len([x for x in tasks if x['session'] == sid]) + 1}"
                    current[sid] = key
                    tasks.append({"task_key": key, "session": sid, "title": title_of(text), "started": ts,
                                  "ended": ts})
                    continue
                if isinstance(msg.get("content"), list):
                    for b in msg["content"]:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in bash_cmds:
                            cmd = bash_cmds.pop(b["tool_use_id"])
                            if is_test_command(cmd) and sid in current:
                                code = 0
                                if b.get("is_error"):
                                    body = b.get("content")
                                    body = body if isinstance(body, str) else json.dumps(body)
                                    m = _EXIT.search(body or "")
                                    code = int(m.group(1)) if m else 1
                                tests.setdefault(current[sid], []).append((ts or 0.0, code, cmd))
                continue
            # assistant
            mid = msg.get("id") or d.get("requestId") or d.get("uuid")
            u = msg.get("usage") or {}
            row = calls.get(mid)
            if row is None:
                inp = u.get("input_tokens") or 0
                cr = u.get("cache_read_input_tokens") or 0
                cw = u.get("cache_creation_input_tokens") or 0
                out = u.get("output_tokens") or 0
                model = msg.get("model")
                row = calls[mid] = {
                    "call_id": f"cc:{mid}", "ts_start": ts, "ts_end": ts, "source": SOURCE, "run_id": "observer",
                    "task_key": current.get(sid), "session_id": sid,
                    "role": "subagent" if d.get("isSidechain") else "agent", "tier": None, "model": model,
                    "api": "messages", "stream": 1, "input_tokens": inp, "cache_read_tokens": cr,
                    "cache_write_tokens": cw, "output_tokens": out,
                    "reasoning_tokens": (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0,
                    "n_tool_calls": 0, "tool_names": [], "status": 200,
                    "cost_usd": pricing.cost_usd(model, inp, cr, cw, out),
                    "meta": {"file": path.name, "account": account},
                }
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    row["tool_names"].append(b.get("name"))
                    row["n_tool_calls"] += 1
                    if b.get("name") == "Bash":
                        bash_cmds[b.get("id")] = (b.get("input") or {}).get("command", "")
            if ts:
                row["ts_end"] = ts
            for x in tasks:
                if x["task_key"] == row["task_key"] and ts:
                    x["ended"] = max(x["ended"] or ts, ts)
    out_tasks = []
    for x in tasks:
        runs = tests.get(x["task_key"], [])
        outcome = UNVERIFIED if not runs else (VERIFIED if runs[-1][1] == 0 else FAILED)
        out_tasks.append({"task_key": x["task_key"], "run_id": "observer", "task_id": x["task_key"],
                          "source": SOURCE, "workload": "coding", "title": x["title"], "started": x["started"],
                          "ended": x["ended"], "outcome": outcome,
                          "meta": {"session": x["session"], "tests": [{"ts": a, "exit": b, "cmd": c[:200]}
                                                                      for a, b, c in runs[-5:]]}})
    return list(calls.values()), out_tasks


def read_limits(config_dir: Path) -> list[dict[str, Any]]:
    """Subscription usage as Claude Code last cached it (five-hour and seven-day windows)."""
    try:
        d = json.loads((config_dir / ".claude.json").read_text())
    except (OSError, ValueError):
        return []
    u = d.get("cachedUsageUtilization") or {}
    out = []
    for window, v in (u.get("utilization") or {}).items():
        if isinstance(v, dict) and v.get("utilization") is not None:
            out.append({"window": window, "used_pct": float(v["utilization"]), "resets_at": _ts(v.get("resets_at")),
                        "ts": (u.get("fetchedAtMs") or 0) / 1000.0 or None})
    return out


def collect(ledger: Ledger, config_dirs: list[str], projects: list[str],
            files: list[Path] | None = None) -> dict[str, int]:
    n_calls = n_tasks = 0
    targets: list[tuple[Path, Path]] = []
    for cd in config_dirs:
        root = Path(cd).expanduser()
        for f in files or sorted((root / "projects").glob("**/*.jsonl")):
            targets.append((root, f))
        label = f"{SOURCE}:{root.name}"
        for lim in read_limits(root):
            ledger.add_limit(label, lim["window"], lim["used_pct"], lim["resets_at"], lim["ts"])
    all_calls, all_tasks = [], []
    for root, f in targets:
        calls, tasks = parse_transcript(f, projects, f"{SOURCE}:{root.name}")
        all_calls += calls
        all_tasks += tasks
    _attach_orphans(all_calls, all_tasks)
    if all_calls:
        n_calls = ledger.add_calls(all_calls)
    for t in all_tasks:
        ledger.upsert_task(t, merge=False)
        n_tasks += 1
    return {"calls": n_calls, "tasks": n_tasks, "files": len(targets)}


def _attach_orphans(calls: list[dict], tasks: list[dict]) -> None:
    """Subagent transcripts live in their own files; book their calls to the parent session's task
    that was active at the time."""
    by_session: dict[str, list[dict]] = {}
    for t in tasks:
        by_session.setdefault(t["meta"]["session"], []).append(t)
    for ts in by_session.values():
        ts.sort(key=lambda t: t["started"] or 0)
    for c in calls:
        if c["task_key"] or not c["session_id"] or c["session_id"] not in by_session:
            continue
        chosen = None
        for t in by_session[c["session_id"]]:
            if (t["started"] or 0) <= (c["ts_start"] or 0):
                chosen = t
        if chosen:
            c["task_key"] = chosen["task_key"]
