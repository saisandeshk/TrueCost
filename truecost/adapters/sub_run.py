"""Frontier tier crash-test on subscription coding agents (Codex / Claude Code), measured passively.

Each (exercise, configuration) runs the official CLI non-interactively in a fresh copy of an Aider Polyglot
exercise, under the user's own login: `codex exec -m <model>` (sandboxed to the exercise folder) or
`claude -p --model <model>` (edits + pytest only). Nothing is routed through the gateway: the agent's own
session log is read afterwards (observer mode) and booked to a ledger under this run and exercise, and the
exercise's ORIGINAL unit tests decide the outcome. Configurations are interleaved per exercise, so stopping
early (rate limits) still leaves paired comparisons.

With --hide-tests the test file is removed while the agent works (it implements from the instructions alone,
as in Aider's first attempt) and put back for grading: frontier models pass nearly every exercise when they can
run the tests, so hidden tests are what separates the tiers on quality.

Limit guards: Codex pauses when its 5-hour window passes --codex-max-pct and stops at its weekly cap;
Claude stops at --claude-max-pct (default 90: the user keeps the last 10%).

Usage:
  python -m truecost.adapters.sub_run --agent codex --configs baseline=gpt-6-astra:medium,luna=gpt-6-luna:medium \
      --exercises ~/truecost-bench/polyglot/python/exercises/practice --limit 20 --root ~/truecost-bench
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from truecost.collectors import claude_code, codex
from truecost.ledger import FAILED, VERIFIED, Ledger

PROMPT = """Solve this programming exercise in Python.

{instructions}

Edit `{stub}` to implement the solution. The tests are in `{test}`; run them with `python3 -m pytest -q {test}`
and iterate until they pass. Do not modify `{test}`. Use only the Python standard library."""

PROMPT_HIDDEN = """Solve this programming exercise in Python.

{instructions}

Edit `{stub}` to implement the solution. Hidden unit tests will import `{stub}` and check it against the
instructions above, so keep its function and class names and signatures. Use only the Python standard library."""

_stop = threading.Event()
_lock = threading.Lock()
_spent = [0.0]  # API-equivalent $ spent by this invocation (budget guard when live limits cannot be read)
_skipped = [0]  # runs that made no model calls


def instructions(ex: Path) -> str:
    return "\n\n".join(p.read_text() for p in (ex / ".docs" / "instructions.md", ex / ".docs" / "instructions.append.md")
                       if p.exists())


def claude_stream_limits(out: str) -> dict:
    """Live usage from `claude -p --output-format stream-json` (its rate_limit_event), in % like Codex."""
    last = {}
    for line in out.splitlines():
        if '"rate_limit_event"' not in line:
            continue
        try:
            info = json.loads(line).get("rate_limit_info") or {}
        except ValueError:
            continue
        for w, v in (info.get("unifiedWindows") or {}).items():
            if isinstance(v, dict) and v.get("utilization") is not None:
                last[w] = round(100 * float(v["utilization"]), 1)
                last[f"{w}_resets"] = v.get("resetsAt")
    return last


def claude_limits(config_dir: str) -> dict:
    try:
        u = json.loads((Path(config_dir).expanduser() / ".claude.json").read_text()).get("cachedUsageUtilization") or {}
        return {k: float(v["utilization"]) for k, v in (u.get("utilization") or {}).items()
                if isinstance(v, dict) and v.get("utilization") is not None}
    except (OSError, ValueError):
        return {}


def codex_limits(rollout: Path | None) -> dict:
    """Latest rate-limit reading (primary = 5-hour, secondary = weekly) from a rollout."""
    if rollout is None or not rollout.exists():
        return {}
    last = {}
    for line in open(rollout, errors="replace"):
        if '"rate_limits"' not in line:
            continue
        try:
            rl = (json.loads(line).get("payload") or {}).get("rate_limits") or {}
        except ValueError:
            continue
        if rl.get("primary"):
            last = {"five_hour": rl["primary"].get("used_percent"), "five_hour_resets": rl["primary"].get("resets_at"),
                    "seven_day": (rl.get("secondary") or {}).get("used_percent"),
                    "seven_day_resets": (rl.get("secondary") or {}).get("resets_at")}
    return last


def run_one(job: dict, a) -> dict:
    ex, run_id, cond, model, effort = job["ex"], job["run_id"], job["cond"], job["model"], job["effort"]
    name, slug = ex.name, ex.name.replace("-", "_")
    stub, test = f"{slug}.py", f"{slug}_test.py"
    wd = Path(a.root).expanduser() / "runs" / run_id / name
    if wd.exists():
        shutil.rmtree(wd)
    shutil.copytree(ex, wd, ignore=shutil.ignore_patterns(".meta"))
    subprocess.run(["git", "init", "-q"], cwd=wd)
    if a.hide_tests:
        (wd / test).unlink(missing_ok=True)
    prompt = (PROMPT_HIDDEN if a.hide_tests else PROMPT).format(instructions=instructions(ex), stub=stub, test=test)
    env = dict(os.environ)
    py = Path(a.py).expanduser()
    env["PATH"] = f"{py.parent}:{env.get('PATH', '')}"  # agents get a python3 with pytest on PATH
    t0 = time.time()
    log_path, out = None, ""
    if a.agent == "codex":
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "-s", "workspace-write", "-c", "approval_policy=never",
               "-c", "features.hooks=false",  # the user's hooks (incl. TrueCost's own face) stay out of measured runs
               "-m", model, "-c", f"model_reasoning_effort={effort}", prompt]
    else:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(a.claude_dir)
        cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits",
               "--allowedTools", "Bash(python3 -m pytest:*)", "Bash(python -m pytest:*)", "Bash(pytest:*)",
               "Read", "Edit", "Write", "Glob", "Grep"]
        if effort and effort != "default":
            cmd += ["--effort", effort]
    try:
        p = subprocess.run(cmd, cwd=wd, env=env, capture_output=True, text=True, timeout=a.timeout,
                           stdin=subprocess.DEVNULL)  # codex exec reads piped stdin and would wait forever
        rc, out = p.returncode, p.stdout  # stdout only: the JSON/JSONL result, not stderr chatter
    except subprocess.TimeoutExpired as e:  # a timed-out run still spent tokens: find its log from the partial output
        rc, out = 124, ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
    dur = time.time() - t0
    shutil.copy(ex / test, wd / test)  # grade against the original tests
    tp = subprocess.run([str(py), "-m", "pytest", "-q", "-x", test], cwd=wd, capture_output=True, text=True, timeout=300)
    passed = tp.returncode == 0
    # find this run's own session log
    if a.agent == "codex":
        tid = next((json.loads(l).get("thread_id") for l in out.splitlines()
                    if l.startswith("{") and '"thread_id"' in l), None)
        cands = glob.glob(os.path.expanduser(f"~/.codex/sessions/**/rollout-*{tid}*.jsonl"), recursive=True) if tid else []
        log_path = Path(cands[0]) if cands else None
    else:
        sid = None
        for line in reversed(out.strip().splitlines()):
            try:
                sid = json.loads(line).get("session_id")
            except (ValueError, AttributeError):
                continue
            if sid:
                break
        cands = glob.glob(os.path.expanduser(f"{a.claude_dir}/projects/*/{sid}.jsonl")) if sid else []
        log_path = Path(cands[0]) if cands else None
    calls, limits = [], {}
    if log_path:
        if a.agent == "codex":
            calls, _, _ = codex.parse_rollout(log_path, [str(wd)])
            limits = codex_limits(log_path)
        else:
            calls, _ = claude_code.parse_transcript(log_path, [str(wd)], f"claude_code:{Path(a.claude_dir).name}")
    if a.agent == "claude":
        limits = claude_stream_limits(out) or claude_limits(a.claude_dir)
    for c in calls:
        c.update({"run_id": run_id, "task_key": f"{run_id}/{name}",
                  "meta": {**(c.get("meta") or {}), "subscription": True, "effort": effort}})
    if rc != 0 and not calls:  # the agent never attempted the task (usage limit, auth, crash): not a FAIL
        with _lock:
            _skipped[0] += 1
            n_skip = _skipped[0]
        print(f"{time.strftime('%H:%M:%S')} {run_id:40s} {name:24s} SKIP rc={rc}: no model calls, not booked "
              f"(retried on resume) {out.strip()[-160:]!r}", flush=True)
        if n_skip >= 3:
            print("STOP: 3 runs made no model calls (usage limit or agent failure?)", flush=True)
            _stop.set()
        return {"run_id": run_id, "exercise": name, "passed": False, "rc": rc, "duration_s": dur, "calls": 0,
                "usd": 0.0, "limits": limits, "skipped": True}
    led = Ledger(a.ledger)
    with _lock:
        if calls:
            led.add_calls(calls)
        led.upsert_task({"task_key": f"{run_id}/{name}", "run_id": run_id, "task_id": name, "source": a.agent,
                         "workload": a.workload, "title": name, "started": t0, "ended": t0 + dur,
                         "outcome": VERIFIED if passed else FAILED, "reward": 1.0 if passed else 0.0,
                         "meta": {"model": model, "effort": effort, "agent_rc": rc, "duration_s": dur,
                                  "log": str(log_path) if log_path else None, "limits_after": limits,
                                  "account": "codex" if a.agent == "codex" else f"claude_code:{Path(a.claude_dir).name}",
                                  "shared_account": a.shared_account}}, merge=False)
        source = "codex" if a.agent == "codex" else f"claude_code:{Path(a.claude_dir).name}"
        for w in ("five_hour", "seven_day"):
            if limits.get(w) is not None:
                led.add_limit(source, w, float(limits[w]), limits.get(f"{w}_resets"))
    usd = sum(c.get("cost_usd") or 0 for c in calls)
    print(f"{time.strftime('%H:%M:%S')} {run_id:40s} {name:24s} {'PASS' if passed else 'fail'} rc={rc} "
          f"{dur:5.0f}s calls={len(calls):3d} ${usd:.3f} limits={limits}", flush=True)
    # guards
    with _lock:
        _spent[0] += usd
    if a.usd_budget and _spent[0] >= a.usd_budget:
        print(f"STOP: API-equivalent spend ${_spent[0]:.2f} reached the ${a.usd_budget:.2f} budget", flush=True)
        _stop.set()
    if a.agent == "claude" and (limits.get("seven_day") or 0) >= a.claude_max_pct:
        print(f"STOP: Claude weekly window at {limits.get('seven_day')}% (cap {a.claude_max_pct}%)", flush=True)
        _stop.set()
    elif a.agent == "claude" and a.claude_5h_pause_pct:
        if ((limits.get("five_hour") or 0) >= a.claude_5h_pause_pct
                and (limits.get("five_hour_resets") or 0) > time.time() and not _stop.is_set()):
            wait = float(limits["five_hour_resets"]) - time.time() + 60
            print(f"PAUSE: Claude 5-hour window at {limits['five_hour']}%: waiting {wait / 60:.0f} min for the reset", flush=True)
            time.sleep(wait)
    elif a.agent == "claude" and (limits.get("five_hour") or 0) >= a.claude_max_pct:
        print(f"STOP: Claude 5-hour window at {limits.get('five_hour')}% (the last {100 - a.claude_max_pct:.0f}% stays free)", flush=True)
        _stop.set()
    if a.agent == "codex":
        if (limits.get("seven_day") or 0) >= a.codex_week_max_pct:
            print(f"STOP: Codex weekly window at {limits.get('seven_day')}%", flush=True)
            _stop.set()
        elif (limits.get("five_hour") or 0) >= a.codex_max_pct and limits.get("five_hour_resets"):
            wait = max(0, float(limits["five_hour_resets"]) - time.time()) + 60
            print(f"PAUSE: Codex 5-hour window at {limits['five_hour']}%: waiting {wait / 60:.0f} min for the reset", flush=True)
            time.sleep(wait)
    return {"run_id": run_id, "exercise": name, "passed": passed, "rc": rc, "duration_s": dur, "calls": len(calls),
            "usd": usd, "limits": limits}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", choices=["codex", "claude"], required=True)
    ap.add_argument("--configs", required=True, help="cond=model:effort,... (e.g. baseline=gpt-6-astra:medium)")
    ap.add_argument("--exercises", required=True)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--root", default="~/truecost-bench")
    ap.add_argument("--ledger", default="~/truecost-bench/ledger.db")
    ap.add_argument("--workload")
    ap.add_argument("--seed", default="1")
    ap.add_argument("--hide-tests", action="store_true", help="agent works without the test file (graded with it)")
    ap.add_argument("--offset", type=int, default=0, help="skip the first N exercises (alphabetical)")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--claude-dir", default="~/.claude-b")
    ap.add_argument("--py", default="~/truecost-bench/venv/bin/python3", help="python with pytest (agents + grading)")
    ap.add_argument("--claude-max-pct", type=float, default=90.0)
    ap.add_argument("--claude-5h-pause-pct", type=float, default=None,
                    help="pause (until the reset) when Claude's 5-hour window reaches this; keeps a shared account usable")
    ap.add_argument("--shared-account", action="store_true",
                    help="the account is also used outside this run (e.g. the builder's own sessions): its usage "
                         "readings are kept out of the plan-meter fit")
    ap.add_argument("--codex-max-pct", type=float, default=95.0)
    ap.add_argument("--codex-week-max-pct", type=float, default=95.0)
    ap.add_argument("--usd-budget", type=float, default=0.0,
                    help="stop after this much API-equivalent spend (a proxy when live limits are not readable)")
    a = ap.parse_args()
    a.workload = a.workload or f"polyglot-{a.agent}-{'hidden' if a.hide_tests else 'frontier'}"
    tag = "hidden-" if a.hide_tests else ""
    configs = []
    for item in a.configs.split(","):
        cond, spec = item.split("=")
        model, _, effort = spec.partition(":")
        configs.append((cond, model, effort or "medium"))
    exs = sorted(p for p in Path(a.exercises).expanduser().iterdir() if p.is_dir())[a.offset: a.offset + a.limit]
    led = Ledger(a.ledger)
    for cond, model, effort in configs:
        led.upsert_run({"run_id": f"sub-{a.agent}-{tag}{cond}-s{a.seed}", "workload": a.workload, "condition": cond,
                        "config": {"agent": a.agent, "model": model, "effort": effort, "subscription": True},
                        "started": time.time()})
    done = {r["task_key"] for r in led.query("SELECT task_key FROM tasks WHERE workload=?", (a.workload,))}
    jobs = [{"ex": ex, "run_id": f"sub-{a.agent}-{tag}{cond}-s{a.seed}", "cond": cond, "model": model, "effort": effort}
            for ex in exs for cond, model, effort in configs
            if f"sub-{a.agent}-{tag}{cond}-s{a.seed}/{ex.name}" not in done]  # resumable
    print(f"{len(jobs)} runs to do ({len(exs)} exercises x {len(configs)} configs, {len(done)} already done)", flush=True)

    if a.agent == "codex":  # a (re)started run must not spend past the cap before its first reading
        logs = sorted(glob.glob(os.path.expanduser("~/.codex/sessions/**/rollout-*.jsonl"), recursive=True),
                      key=os.path.getmtime)[-3:]
        lim = {}
        for f in logs:
            lim = codex_limits(Path(f)) or lim
        if (lim.get("seven_day") or 0) >= a.codex_week_max_pct and (lim.get("seven_day_resets") or 0) > time.time():
            print(f"STOP: Codex weekly window at {lim.get('seven_day')}%", flush=True)
            return 0
        if (lim.get("five_hour") or 0) >= a.codex_max_pct and (lim.get("five_hour_resets") or 0) > time.time():
            wait = float(lim["five_hour_resets"]) - time.time() + 60
            print(f"PAUSE at start: Codex 5-hour window at {lim['five_hour']}%: waiting {wait / 60:.0f} min", flush=True)
            time.sleep(wait)

    def guarded(job):
        if _stop.is_set():
            return None
        return run_one(job, a)

    with ThreadPoolExecutor(a.jobs) as pool:
        results = [r for r in pool.map(guarded, jobs) if r]
    for cond, *_ in configs:
        rs = [r for r in results if r["run_id"].endswith(f"-{cond}-s{a.seed}")]
        if rs:
            print(f"{cond:12s} {sum(r['passed'] for r in rs)}/{len(rs)} passed, ${sum(r['usd'] for r in rs):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
