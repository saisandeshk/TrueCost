"""truecost command line.

  truecost serve --config gw.toml [--run R] [--saver mask:keep_last=8] [--port 30020]
  truecost import-tau2 results.json --run R [--workload tau2-retail]
  truecost import-autobench export.json --run R
  truecost crashtest grids/retail.toml [--only baseline mask]
  truecost report R [R ...] [--baseline B] [--json]
  truecost demo [--download-only]
  truecost prices
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _ledger(path):
    from .ledger import Ledger

    return Ledger(path)


def cmd_serve(a):
    import uvicorn

    from .config import Config
    from .gateway import create_app
    from .savers import parse_cli

    cfg = Config.load(a.config)
    for field in ("run", "workload", "condition", "db"):
        if getattr(a, field, None):
            setattr(cfg, field, getattr(a, field))
    if a.saver:
        cfg.savers = [parse_cli(s) for s in a.saver]
    if a.governor:
        cfg.governor = json.loads(a.governor)
    if a.no_nvml:
        cfg.nvml_gpus = []
    uvicorn.run(create_app(cfg), host=a.host, port=a.port, log_level=a.log_level, access_log=False)


def cmd_import_tau2(a):
    from .report import import_tau2

    n = import_tau2(_ledger(a.db), a.results, a.run, a.workload)
    print(f"imported {n} tasks into run {a.run}")


def cmd_import_autobench(a):
    from .report import import_autobench

    n = import_autobench(_ledger(a.db), a.results, a.run, a.workload)
    print(f"imported {n} tasks into run {a.run}")


def cmd_crashtest(a):
    from .crashtest import Grid

    Grid(a.grid).run(a.only, a.seeds)


def cmd_report(a):
    from .report import compare, fmt_metrics, run_metrics

    led = _ledger(a.db)
    ms = [run_metrics(led, r) for r in a.runs]
    cmps = [compare(led, a.baseline, r) for r in a.runs if a.baseline and r != a.baseline]
    if a.json:
        print(json.dumps({"runs": ms, "comparisons": cmps}, indent=1, default=str))
        return
    print(fmt_metrics(ms))
    def r2(x):
        return "n/a" if x is None or x != x else f"{x:.2f}"

    for c in cmps:
        if not c.get("paired_tasks"):
            continue
        print(f"\n{c['run']} vs {c['base']} on {c['paired_tasks']} paired tasks: "
              f"success {c['success_base']:.1%} -> {c['success_run']:.1%} ({c['success_delta_pp']:+.1f} pp, "
              f"lost {c['lost']} / gained {c['gained']}, p = {c['p_value']:.3f}); "
              f"$ per verified x{r2(c['usd_per_verified_ratio'])} "
              f"[{r2(c['usd_per_verified_ratio_ci95'][0])}, {r2(c['usd_per_verified_ratio_ci95'][1])}]; "
              f"energy per verified x{r2(c['energy_per_verified_ratio'])} "
              f"[{r2(c['energy_per_verified_ratio_ci95'][0])}, {r2(c['energy_per_verified_ratio_ci95'][1])}]; "
              f"tokens per task x{r2(c['tokens_per_task_ratio'])}")


def cmd_status(a):
    from .agentface import main_status

    try:
        print(main_status(a.agent, sys.stdin.read() if not sys.stdin.isatty() else "", a.db))
    except Exception as e:  # a statusline must never crash the agent's UI
        print(f"TrueCost: {type(e).__name__}")


def cmd_hook(a):
    from .agentface import hook
    from .ledger import Ledger

    try:
        payload = json.loads(sys.stdin.read() or "{}")
        out = hook(a.event, a.agent, payload, Ledger(a.db))
    except Exception:
        return  # hooks fail open: never block the agent because TrueCost broke
    if out:
        print(json.dumps(out))


def cmd_receipt(a):
    from .agentface import receipt, refresh
    from .ledger import Ledger

    led = Ledger(a.db)
    if a.transcript or a.session:
        refresh(led, a.agent, a.transcript, a.session)
    if a.task:
        key = a.task
    else:
        src = "claude_code" if a.agent == "claude" else "codex"
        q = "SELECT task_key FROM tasks WHERE source=?" + (" AND task_key LIKE ?" if a.session else "")
        args = [src] + ([f"%:{a.session}:%"] if a.session else [])
        r = led.one(q + " ORDER BY started DESC LIMIT 1", args)
        key = r["task_key"] if r else ""
    print(receipt(led, key))


def cmd_collect(a):
    from .collectors import claude_code, codex
    from .ledger import Ledger

    led = Ledger(a.db)
    projects = a.projects or [os.getcwd()]
    if a.claude_dirs:
        print("claude_code:", claude_code.collect(led, a.claude_dirs, projects))
    if a.codex:
        print("codex:", codex.collect(led, projects=projects))


def cmd_console(a):
    import uvicorn

    from .console import create_console

    dbs = a.ledger or [a.db]
    uvicorn.run(create_console(dbs, a.gates), host=a.host, port=a.port, log_level="warning", access_log=False)


RESULTS_URL = "https://github.com/saisandeshk/TrueCost/releases/download/v0.1.0/truecost-results.db.xz"


def cmd_demo(a):
    """Download the published results ledger (every evaluation run) once, then open the console on it."""
    import lzma
    from pathlib import Path

    import httpx

    dest = Path(a.path).expanduser()
    if a.refresh or not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        print(f"downloading the results ledger from {RESULTS_URL} (about 30 MB) ...", file=sys.stderr)
        unxz = lzma.LZMADecompressor()
        with httpx.stream("GET", RESULTS_URL, follow_redirects=True, timeout=120) as r, open(tmp, "wb") as f:
            r.raise_for_status()
            for chunk in r.iter_bytes():
                f.write(unxz.decompress(chunk))
        tmp.replace(dest)
        print(f"saved {dest}", file=sys.stderr)
    if a.download_only:
        print(dest)
        return
    import uvicorn

    from .console import create_console

    print(f"TrueCost console on the published results: http://{a.host}:{a.port}", file=sys.stderr)
    uvicorn.run(create_console([str(dest)], None), host=a.host, port=a.port, log_level="warning", access_log=False)


def cmd_calibrate(a):
    from .calibrate import main as calibrate_main

    calibrate_main(a.rest)


def cmd_init(a):
    from pathlib import Path

    from .installer import init

    print(init(a.agent, a.scope, a.write, a.breaker, Path(a.project).resolve()))


def cmd_board(a):
    from .console import Console
    from .ratings import glyphs

    con = Console(a.ledger or [a.db])
    d = con.crashtest(a.workload, subset=a.subset)
    if not d.get("rows"):
        print(d.get("error", "no runs yet"))
        return
    fam = ["reliability", "safety", "security", "sustainability", "cost"]
    print(f"### {a.workload}: crash-test vs `{d['baseline']}` (protocol {d['protocol']})\n")
    print("| Trick | runs | " + " | ".join(f.capitalize() for f in fam) +
          " | Verified | Δ success (95% CI) | pass^k | $/verified | Energy/verified | Violations/100 |")
    print("|---|---|" + "---|" * len(fam) + "---|---|---|---|---|---|")
    f2 = lambda x, n=2: "–" if x is None else f"{x:.{n}f}"
    for r in d["rows"]:
        c = r["compare"]
        ci = c.get("success_delta_ci95") or [None, None]
        delta = "–" if r["condition"] == d["baseline"] else \
            f"{c['success_delta_pp']:+.1f} ({f2(ci[0], 1)} to {f2(ci[1], 1)})"
        prov = " (provisional)" if r.get("provisional") else ""
        print(f"| {r['condition']}{prov} | {c.get('runs', 1)} | " + " | ".join(glyphs(r["stars"][f]) for f in fam) +
              f" | {100 * c['success_run']:.0f}% | {delta} | "
              f"{'–' if not r.get('consistency') else str(round(100 * r['consistency']['pass_all'])) + '% (k=' + str(r['consistency']['k']) + ')'}"
              f" | ${f2(c.get('usd_per_verified_run'), 4)} ×{f2(c.get('usd_per_verified_ratio'))}"
              f" | {f2(c.get('wh_per_verified_run'))} Wh ×{f2(c.get('energy_per_verified_ratio'))}"
              f" | {f2(c.get('violations_run_per_100'), 1)} |")
    nf = d.get("noise_floor")
    if nf:
        print(f"\nNoise floor: baseline runs differ by {nf['mean_abs_delta_pp']:.1f} pp on average "
              f"(max {nf['max_abs_delta_pp']:.1f} pp over {nf['pairs']} pairs).")


def cmd_reprice(a):
    from . import pricing
    from .ledger import Ledger

    led = Ledger(a.db)
    rows = led.query("SELECT call_id, model, input_tokens, cache_read_tokens, cache_write_tokens, output_tokens FROM calls"
                     + (" WHERE run_id=?" if a.run else ""), [a.run] if a.run else [])
    c = led.conn
    c.execute("BEGIN")
    for r in rows:
        c.execute("UPDATE calls SET cost_usd=? WHERE call_id=?",
                  (pricing.cost_usd(r["model"], r["input_tokens"] or 0, r["cache_read_tokens"] or 0,
                                    r["cache_write_tokens"] or 0, r["output_tokens"] or 0), r["call_id"]))
    c.execute("COMMIT")
    print(f"repriced {len(rows)} calls with the {pricing.snapshot_date()} snapshot")


def cmd_prices(a):
    from . import pricing

    t = pricing._table()
    print(f"snapshot {t['snapshot']} ({t['unit']})")
    for k, v in t["models"].items():
        print(f"  {k:<40} in {v['in']:<7} out {v['out']:<7} cache {v['cache_read']}  [{v['source']}]")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="truecost")
    ap.add_argument("--db", default="~/.truecost/ledger.db")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the gateway")
    s.add_argument("--config", required=True)
    s.add_argument("--run")
    s.add_argument("--workload")
    s.add_argument("--condition")
    s.add_argument("--saver", action="append", help="name:key=val,... (repeatable; replaces config savers)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=30020)
    s.add_argument("--governor", help="governor spec as JSON (overrides config)")
    s.add_argument("--no-nvml", action="store_true")
    s.add_argument("--log-level", default="warning")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("import-tau2", help="record τ² outcomes in the ledger")
    s.add_argument("results")
    s.add_argument("--run", required=True)
    s.add_argument("--workload")
    s.set_defaults(fn=cmd_import_tau2)

    s = sub.add_parser("import-autobench", help="record AutomationBench outcomes in the ledger")
    s.add_argument("results")
    s.add_argument("--run", required=True)
    s.add_argument("--workload")
    s.set_defaults(fn=cmd_import_autobench)

    s = sub.add_parser("crashtest", help="run a crash-test grid (resumable)")
    s.add_argument("grid")
    s.add_argument("--only", nargs="*")
    s.add_argument("--seeds", nargs="*", type=int)
    s.set_defaults(fn=cmd_crashtest)

    s = sub.add_parser("report", help="metrics per run, paired comparisons vs a baseline")
    s.add_argument("runs", nargs="+")
    s.add_argument("--baseline")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("status", help="one-line status for a coding agent's statusline (JSON on stdin)")
    s.add_argument("--agent", choices=["claude", "codex"], default="claude")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("hook", help="agent hook handler (JSON on stdin): stop | pretooluse")
    s.add_argument("event", choices=["stop", "pretooluse"])
    s.add_argument("--agent", choices=["claude", "codex"], default="claude")
    s.set_defaults(fn=cmd_hook)

    s = sub.add_parser("receipt", help="cost receipt for the latest (or a given) coding task")
    s.add_argument("--agent", choices=["claude", "codex"], default="claude")
    s.add_argument("--session")
    s.add_argument("--transcript")
    s.add_argument("--task")
    s.set_defaults(fn=cmd_receipt)

    s = sub.add_parser("collect", help="read coding-agent session logs into the ledger")
    s.add_argument("--claude-dirs", nargs="*", default=[])
    s.add_argument("--codex", action="store_true")
    s.add_argument("--projects", nargs="*")
    s.set_defaults(fn=cmd_collect)

    s = sub.add_parser("console", help="serve the web console over one or more ledgers")
    s.add_argument("--ledger", action="append", help="ledger file (repeatable); defaults to --db")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8686)
    s.add_argument("--gates", help="directory of calibrated gate files (default: <first ledger dir>/gates)")
    s.set_defaults(fn=cmd_console)

    s = sub.add_parser("demo", help="open the console on the published results (downloads them once)")
    s.add_argument("--path", default="~/.truecost/results.db")
    s.add_argument("--download-only", action="store_true")
    s.add_argument("--refresh", action="store_true", help="download again")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8686)
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("calibrate", help="fit the governor gate and conformal thresholds from twin files")
    s.add_argument("rest", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_calibrate)

    s = sub.add_parser("init", help="install the in-agent face into Claude Code or Codex (dry run by default)")
    s.add_argument("agent", choices=["claude-code", "codex"])
    s.add_argument("--scope", choices=["project", "user"], default="project")
    s.add_argument("--project", default=".")
    s.add_argument("--breaker", action="store_true", help="also add the PreToolUse breaker hook")
    s.add_argument("--write", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("board", help="crash-test board for a workload, as markdown")
    s.add_argument("workload")
    s.add_argument("--ledger", action="append")
    s.add_argument("--subset", choices=["gov"])
    s.set_defaults(fn=cmd_board)

    s = sub.add_parser("reprice", help="recompute every call's $ from its tokens with the current price snapshot")
    s.add_argument("--run")
    s.set_defaults(fn=cmd_reprice)

    s = sub.add_parser("prices", help="show the price snapshot")
    s.set_defaults(fn=cmd_prices)

    a = ap.parse_args(argv)
    if a.cmd == "serve" and a.db == ap.get_default("db"):
        a.db = None  # take the config's db unless --db was given
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
