"""Coding crash-test workload: Aider's Polyglot benchmark (Python exercises), solved by Codex or Claude Code
running headless against the TrueCost gateway, verified by the exercise's own unit tests.

Each exercise runs in a fresh copy. The agent's calls carry X-TrueCost-Task=<exercise> (Codex: provider
http_headers; Claude Code: ANTHROPIC_CUSTOM_HEADERS). After the agent stops, the ORIGINAL test file is
restored (so editing the tests cannot pass) and pytest decides the outcome. Agent homes are isolated
temp dirs, so no personal config, login or hooks are used.

Usage:
  python polyglot_run.py --agent codex|claude --gateway http://127.0.0.1:30020 --run R \
      --exercises ~/polyglot-benchmark/python/exercises/practice --out results.json [--limit N] [--jobs 2]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROMPT = """Solve this programming exercise in Python.

{instructions}

Edit `{stub}` to implement the solution. The tests are in `{test}`; run them with `python -m pytest -q {test}`
and iterate until they pass. Do not modify `{test}`. Use only the Python standard library."""


def instructions(ex: Path) -> str:
    parts = []
    for name in ("instructions.md", "instructions.append.md"):
        p = ex / ".docs" / name
        if p.exists():
            parts.append(p.read_text())
    return "\n\n".join(parts)


def run_one(ex: Path, a, bin_dir: Path) -> dict:
    name = ex.name
    slug = name.replace("-", "_")
    stub, test = f"{slug}.py", f"{slug}_test.py"
    work = Path(tempfile.mkdtemp(prefix=f"pg-{name}-"))
    home = Path(tempfile.mkdtemp(prefix="pg-home-"))
    try:
        shutil.copytree(ex, work / name, ignore=shutil.ignore_patterns(".meta"))
        wd = work / name
        subprocess.run(["git", "init", "-q"], cwd=wd)
        prompt = PROMPT.format(instructions=instructions(ex), stub=stub, test=test)
        py_bin = str(Path(sys.executable).parent)  # a python3 with pytest, for the agent and for grading
        env = {"PATH": f"{bin_dir}:{py_bin}:{os.environ.get('PATH', '')}", "HOME": str(home), "LANG": "C.UTF-8",
               "TERM": "dumb"}
        if a.agent == "codex":
            env["CODEX_HOME"] = str(home / ".codex")
            (home / ".codex").mkdir(parents=True, exist_ok=True)  # codex refuses a CODEX_HOME that does not exist
            env["TRUECOST_KEY"] = "local"
            provider = (f'model_providers.truecost={{name="TrueCost",base_url="{a.gateway}/v1",wire_api="responses",'
                        f'env_key="TRUECOST_KEY",http_headers={{"X-TrueCost-Task"="{name}","X-TrueCost-Run"="{a.run}"}}}}')
            cmd = [str(bin_dir / "codex"), "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
                   "-c", provider, "-c", "model_provider=truecost", "-m", a.model, prompt]
        else:
            env.update({"ANTHROPIC_BASE_URL": a.gateway, "ANTHROPIC_API_KEY": "local",
                        "ANTHROPIC_CUSTOM_HEADERS": f"X-TrueCost-Task: {name}\nX-TrueCost-Run: {a.run}",
                        "CLAUDE_CONFIG_DIR": str(home / ".claude"), "DISABLE_TELEMETRY": "1",
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"})
            cmd = [str(bin_dir / "claude"), "-p", prompt, "--dangerously-skip-permissions", "--model", a.model]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=wd, env=env, capture_output=True, text=True, timeout=a.timeout,
                               stdin=subprocess.DEVNULL)  # codex exec reads piped stdin and would wait forever
            rc, tail = p.returncode, (p.stdout + p.stderr)[-1500:]
        except subprocess.TimeoutExpired as e:
            rc, tail = 124, f"timeout after {a.timeout}s"
        dur = time.time() - t0
        shutil.copy(ex / test, wd / test)  # restore the original tests before grading
        tp = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", test], cwd=wd, capture_output=True, text=True,
                            timeout=300)
        return {"name": name, "passed": tp.returncode == 0, "agent_rc": rc, "duration_s": dur,
                "test_tail": tp.stdout[-400:], "agent_tail": tail[-600:]}
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", choices=["codex", "claude"], required=True)
    ap.add_argument("--gateway", required=True, help="gateway root, e.g. http://127.0.0.1:30020")
    ap.add_argument("--run", required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--exercises", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--bin", default="~/.truecost/bin")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()
    a.model = a.model or ("strong" if a.agent == "codex" else "claude-opus-5-5")
    exs = sorted(p for p in Path(a.exercises).expanduser().iterdir() if p.is_dir())
    if a.only:
        exs = [e for e in exs if e.name in a.only]
    if a.limit:
        exs = exs[: a.limit]
    bin_dir = Path(a.bin).expanduser()
    with ThreadPoolExecutor(a.jobs) as pool:
        results = list(pool.map(lambda e: run_one(e, a, bin_dir), exs))
    Path(a.out).write_text(json.dumps({"agent": a.agent, "run": a.run, "tasks": results}, indent=1))
    print(f"{sum(r['passed'] for r in results)}/{len(results)} exercises passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
