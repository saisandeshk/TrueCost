"""Codex collector: rollouts in ~/.codex/sessions/**/rollout-*.jsonl -> ledger calls and tasks.

A task is one Codex turn (task_started .. task_complete). Usage comes from token_usage_record lines
(one per model response; input_tokens include cached ones). Test runs are found in exec_command calls
(inside the JS `exec` tool or as direct function calls) and their exit codes in the matching outputs.
Rate-limit windows (five-hour and weekly) come from token_count events.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .. import pricing
from ..ledger import FAILED, UNVERIFIED, VERIFIED, Ledger
from . import is_test_command, title_of, under

SOURCE = "codex"
_CMD = re.compile(r"""\bcmd\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)""")
_EXIT = re.compile(r'\\?"exit_code\\?"\s*:\s*(-?\d+)')


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
    except ValueError:
        return None


def _unquote(s: str) -> str:
    if s[0] == '"':
        try:
            return json.loads(s)
        except ValueError:
            return s[1:-1]
    return s[1:-1]


def _commands(payload: dict) -> list[str]:
    if payload.get("type") == "custom_tool_call":
        return [_unquote(m.group(1)) for m in _CMD.finditer(payload.get("input") or "")]
    if payload.get("type") == "function_call":
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except ValueError:
            return []
        cmd = args.get("cmd") or args.get("command")
        if isinstance(cmd, list):
            cmd = " ".join(map(str, cmd))
        return [cmd] if isinstance(cmd, str) else []
    return []


def _output_text(payload: dict) -> str:
    out = payload.get("output")
    if isinstance(out, list):
        return "\n".join(o.get("text", "") for o in out if isinstance(o, dict))
    return out if isinstance(out, str) else json.dumps(out)


def parse_rollout(path: Path, projects: list[str]) -> tuple[list[dict], list[dict], list[dict]]:
    calls, tasks, limits = [], [], []
    pending: dict[str, list[str]] = {}
    tests: dict[str, list[tuple[float, int, str]]] = {}
    session, model, turn = path.stem, None, None
    first_user: dict[str, str] = {}
    allowed = True  # outside the allow-list we still read account-wide rate limits, never content or calls
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t, p, ts = d.get("type"), d.get("payload") or {}, _ts(d.get("timestamp"))
            if t == "session_meta":
                session = p.get("session_id") or p.get("id") or session
                allowed = under(p.get("cwd"), projects)
                continue
            if t == "turn_context":
                model = p.get("model") or model
                continue
            if t == "event_msg":
                et = p.get("type")
                if et == "thread_settings_applied":
                    model = (p.get("thread_settings") or {}).get("model") or model
                elif et == "task_started" and allowed:
                    turn = p.get("turn_id")
                    tasks.append({"task_key": f"codex:{session}:{turn}", "turn": turn, "started": ts, "ended": ts})
                elif et == "task_complete" and tasks and allowed:
                    tasks[-1]["ended"] = ts
                elif et == "token_count" and p.get("rate_limits"):
                    rl = p["rate_limits"]
                    for w in ("primary", "secondary"):
                        v = rl.get(w)
                        if v and v.get("used_percent") is not None:
                            name = "five_hour" if v.get("window_minutes") == 300 else (
                                "seven_day" if v.get("window_minutes") == 10080 else f"{v.get('window_minutes')}m")
                            limits.append({"window": name, "used_pct": float(v["used_percent"]),
                                           "resets_at": v.get("resets_at"), "ts": ts})
                elif et == "user_message" and allowed and turn and turn not in first_user:
                    first_user[turn] = p.get("message") or ""
                continue
            if not allowed:
                continue
            if t == "token_usage_record":
                u = p.get("usage") or {}
                cached = u.get("cached_input_tokens") or 0
                inp = max(0, (u.get("input_tokens") or 0) - cached)
                cw = u.get("cache_write_input_tokens") or 0
                out = u.get("output_tokens") or 0
                tk = f"codex:{session}:{p.get('turn_id') or turn}"
                calls.append({"call_id": f"codex:{p.get('response_id') or ts}", "ts_start": ts, "ts_end": ts,
                              "source": SOURCE, "run_id": "observer", "task_key": tk, "session_id": session,
                              "role": "agent", "model": model, "api": "responses", "stream": 1,
                              "input_tokens": inp, "cache_read_tokens": cached, "cache_write_tokens": cw,
                              "output_tokens": out, "reasoning_tokens": u.get("reasoning_output_tokens") or 0,
                              "status": 200, "cost_usd": pricing.cost_usd(model, inp, cached, cw, out),
                              "meta": {"file": path.name, "account": SOURCE}})
                continue
            if t == "response_item":
                pt = p.get("type")
                if pt == "message" and p.get("role") == "user" and turn and turn not in first_user:
                    txt = " ".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict))
                    if txt and not txt.lstrip().startswith("<"):
                        first_user[turn] = txt
                elif pt in ("custom_tool_call", "function_call"):
                    cmds = _commands(p)
                    if cmds:
                        pending[p.get("call_id")] = cmds
                elif pt in ("custom_tool_call_output", "function_call_output") and p.get("call_id") in pending:
                    cmds = pending.pop(p["call_id"])
                    codes = [int(x) for x in _EXIT.findall(_output_text(p))]
                    for i, cmd in enumerate(cmds):
                        if is_test_command(cmd) and turn:
                            code = codes[i] if i < len(codes) else (codes[-1] if codes else 1)
                            tests.setdefault(f"codex:{session}:{turn}", []).append((ts or 0.0, code, cmd))
    out_tasks = []
    for x in tasks:
        runs = tests.get(x["task_key"], [])
        outcome = UNVERIFIED if not runs else (VERIFIED if runs[-1][1] == 0 else FAILED)
        out_tasks.append({"task_key": x["task_key"], "run_id": "observer", "task_id": x["turn"], "source": SOURCE,
                          "workload": "coding", "title": title_of(first_user.get(x["turn"], "") or "(turn)"),
                          "started": x["started"], "ended": x["ended"], "outcome": outcome,
                          "meta": {"session": session, "tests": [{"ts": a, "exit": b, "cmd": c[:200]}
                                                                 for a, b, c in runs[-5:]]}})
    return calls, out_tasks, limits


def collect(ledger: Ledger, sessions_dir: str = "~/.codex/sessions", projects: list[str] | None = None,
            files: list[Path] | None = None) -> dict[str, int]:
    projects = projects or []
    files = files or sorted(Path(sessions_dir).expanduser().glob("**/rollout-*.jsonl"))
    n_calls = n_tasks = 0
    for f in files:
        calls, tasks, limits = parse_rollout(f, projects)
        if calls:
            n_calls += ledger.add_calls(calls)
        for t in tasks:
            ledger.upsert_task(t, merge=False)
            n_tasks += 1
        for lim in limits:
            ledger.add_limit(SOURCE, lim["window"], lim["used_pct"], lim["resets_at"], lim["ts"])
    return {"calls": n_calls, "tasks": n_tasks, "files": len(files)}
