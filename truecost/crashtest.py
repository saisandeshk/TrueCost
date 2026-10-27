"""Crash-test runner: run a grid of (cell x seed) runs unattended, one at a time, resumably.

For each run it starts a gateway with that cell's savers (and governor), runs the workload through its
adapter, stops the gateway cleanly (so the run's GPU energy is written), imports the graded outcomes,
and records the run in <out>/state.json. Completed runs are skipped on restart; a failed run is
logged and the grid moves on. Progress lines go to <out>/PROGRESS and <out>/DONE marks the end.

Grid file (TOML):
    [grid]
    name = "retail"
    db = "~/.truecost/ledger.db"
    gateway_config = "evaluation/configs/h100.toml"
    out = "~/.truecost/grids/retail"
    port = 30020

    [workload]
    kind = "tau2"                 # or "autobench"
    domain = "retail"
    split = "test"                # optional
    concurrency = 8
    max_steps = 60
    ensure_complete = false       # autobench: false = an aborted rollout counts as failed (no re-run rounds)
    ...

    [[cells]]
    name = "baseline"
    savers = []                   # e.g. ["mask:keep_last=8"]
    seeds = [300, 301, 302]
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _x(p: str | None) -> str | None:
    return os.path.expanduser(p) if p else p


class Grid:
    def __init__(self, path: str):
        with open(_x(path), "rb") as f:
            self.spec = tomllib.load(f)
        g = self.spec["grid"]
        self.name = g["name"]
        self.db = _x(g["db"])
        # a machine-wide override (e.g. the single-GPU sandbox) without editing every grid
        self.gw_config = _x(os.environ.get("TRUECOST_GATEWAY_CONFIG") or g["gateway_config"])
        self.out = Path(_x(g["out"]))
        self.port = int(g.get("port", 30020))
        self.python = _x(g.get("python")) or sys.executable
        self.wl = self.spec["workload"]
        self.cells = self.spec["cells"]
        self.out.mkdir(parents=True, exist_ok=True)
        self.state_path = self.out / "state.json"
        self.state: dict[str, Any] = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    # ---- bookkeeping ----------------------------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"{time.strftime('%F %T')} {msg}"
        print(line, flush=True)
        with open(self.out / "PROGRESS", "a") as f:
            f.write(line + "\n")

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.state_path)

    def runs(self) -> list[tuple[dict, int, str]]:
        out = []
        for cell in self.cells:
            for seed in cell.get("seeds", [300]):
                out.append((cell, seed, f"{self.name}-{cell['name']}-s{seed}"))
        return out

    # ---- gateway --------------------------------------------------------------------------------
    def port_free(self, wait_s: float = 60.0) -> bool:
        """True once nothing listens on the gateway port (a stale gateway would serve the wrong run and code)."""
        import socket

        deadline = time.time() + wait_s
        while time.time() < deadline:
            with socket.socket() as s:
                s.settimeout(1)
                if s.connect_ex(("127.0.0.1", self.port)) != 0:
                    return True
            time.sleep(1)
        return False

    def start_gateway(self, run_id: str, cell: dict, rundir: Path) -> subprocess.Popen:
        if not self.port_free():
            raise RuntimeError(f"port {self.port} still in use by another gateway")
        cmd = [self.python, "-m", "truecost.cli", "--db", self.db, "serve", "--config", self.gw_config,
               "--run", run_id, "--workload", self.wl.get("workload_name", self.wl["kind"]),
               "--condition", cell["name"], "--port", str(self.port)]
        for s in cell.get("savers", []):
            cmd += ["--saver", s]
        if cell.get("governor"):
            cmd += ["--governor", json.dumps(cell["governor"])]
        log = open(rundir / "gateway.log", "w")
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        url = f"http://127.0.0.1:{self.port}/health"
        for _ in range(120):
            if p.poll() is not None:
                raise RuntimeError(f"gateway exited early (see {rundir / 'gateway.log'})")
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        return p
            except OSError:
                time.sleep(0.5)
        p.kill()
        raise RuntimeError("gateway did not become healthy")

    @staticmethod
    def stop_gateway(p: subprocess.Popen) -> None:
        if p.poll() is None:
            p.send_signal(signal.SIGINT)  # graceful: lifespan shutdown writes the run's energy
            try:
                p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                p.kill()

    # ---- workloads ------------------------------------------------------------------------------
    def workload_cmd(self, run_id: str, seed: int, rundir: Path) -> tuple[list[str], str, dict]:
        wl = self.wl
        gw = f"http://127.0.0.1:{self.port}/v1"
        env = dict(os.environ)
        if wl["kind"] == "tau2":
            llm_args = json.dumps({"api_base": gw, "api_key": "local", "temperature": 0.0,
                                   "max_tokens": int(wl.get("max_tokens", 6144))})
            cmd = [_x(wl["python"]), str(HERE / "adapters" / "tau2_run.py"), "--gateway", gw, "--run", run_id, "--",
                   "run", "--domain", wl["domain"], "--agent-llm", f"openai/{wl.get('agent_model', 'strong')}",
                   "--agent-llm-args", llm_args, "--user-llm", "openai/sim", "--user-llm-args", llm_args,
                   "--num-trials", str(wl.get("trials", 1)), "--max-concurrency", str(wl.get("concurrency", 8)),
                   "--seed", str(seed), "--max-steps", str(wl.get("max_steps", 60)),
                   "--save-to", str(rundir / "results.json")]
            if wl.get("split"):
                cmd += ["--task-split-name", wl["split"]]
            if wl.get("num_tasks"):
                cmd += ["--num-tasks", str(wl["num_tasks"])]
            if wl.get("task_ids"):
                cmd += ["--task-ids", *map(str, wl["task_ids"])]
            cmd += [str(x) for x in wl.get("extra_args", [])]  # e.g. τ³ banking: --retrieval-config bm25
            if wl.get("data_dir"):
                env["TAU2_DATA_DIR"] = _x(wl["data_dir"])
            return cmd, _x(wl["dir"]), env
        if wl["kind"] == "autobench":
            cmd = [_x(wl["python"]), str(HERE / "adapters" / "autobench_run.py"),
                   "--model", wl.get("agent_model", "strong"), "--base-url", gw, "--api-key", "local",
                   "--domains", wl.get("domains", "finance"), "--headers", f"X-TrueCost-Run={run_id}",
                   "--headers", "X-TrueCost-Role=agent", "--export-json", str(rundir / "results.json"),
                   "--max-concurrent", str(wl.get("concurrency", 8)), "--max-steps", str(wl.get("max_steps", 50))]
            if wl.get("num_tasks"):
                cmd += ["--num-examples", str(wl["num_tasks"])]
            if wl.get("task_ids"):
                cmd += ["--tasks", ",".join(wl["task_ids"])]
            if not wl.get("ensure_complete", True):
                cmd += ["--no-ensure-complete"]
            env.setdefault("OPENAI_API_KEY", "local")
            return cmd, _x(wl["dir"]), env
        if wl["kind"] == "command":  # any harness: a command template + the format of the results it writes
            subst = {"gateway": gw, "gateway_root": f"http://127.0.0.1:{self.port}", "run": run_id, "seed": str(seed),
                     "out": str(rundir / "results.json")}
            cmd = [str(c).format(**subst) for c in wl["cmd"]]
            return cmd, _x(wl.get("dir")) or str(rundir), env
        if wl["kind"] == "polyglot":
            cmd = [_x(wl.get("python", "python3")), str(HERE / "adapters" / "polyglot_run.py"), "--agent", wl["agent"],
                   "--gateway", f"http://127.0.0.1:{self.port}", "--run", run_id, "--exercises", _x(wl["exercises"]),
                   "--out", str(rundir / "results.json"), "--jobs", str(wl.get("concurrency", 2)),
                   "--timeout", str(wl.get("timeout", 900))]
            if wl.get("model"):
                cmd += ["--model", wl["model"]]
            if wl.get("num_tasks"):
                cmd += ["--limit", str(wl["num_tasks"])]
            if wl.get("task_ids"):
                cmd += ["--only", *wl["task_ids"]]
            return cmd, str(Path(_x(wl["exercises"])).parent), env
        raise ValueError(f"unknown workload kind {wl['kind']!r}")

    def import_results(self, run_id: str, rundir: Path) -> int:
        from .ledger import Ledger
        from .report import import_autobench, import_polyglot, import_tau2

        res = rundir / "results.json"
        if res.is_dir():  # τ² may write a directory
            files = sorted(res.glob("*.json"))
            res = files[0] if files else res
        led = Ledger(self.db)
        name = self.wl.get("workload_name", self.wl["kind"])
        kind = self.wl.get("format", self.wl["kind"]) if self.wl["kind"] == "command" else self.wl["kind"]
        if kind == "tau2":
            return import_tau2(led, str(res), run_id, name)
        if kind == "polyglot":
            return import_polyglot(led, str(res), run_id, name)
        return import_autobench(led, str(res), run_id, name)

    def expected_tasks(self) -> int | None:
        """How many graded tasks a complete run must have (None when it cannot be known up front)."""
        wl = self.wl
        n = wl.get("num_tasks")
        if wl["kind"] == "tau2" and wl.get("data_dir"):
            base = Path(_x(wl["data_dir"])) / "tau2" / "domains" / wl["domain"]
            try:
                if wl.get("task_ids"):
                    total = len(wl["task_ids"])
                elif wl.get("split"):
                    total = len(json.loads((base / "split_tasks.json").read_text())[wl["split"]])
                else:
                    total = len(json.loads((base / "tasks.json").read_text()))
            except (OSError, ValueError, KeyError):
                return n
            return min(total, n) * int(wl.get("trials", 1)) if n else total * int(wl.get("trials", 1))
        return n

    def upstreams_healthy(self, cell: dict | None = None) -> list[str]:
        """Names of the upstreams this cell needs whose /health does not answer (checked before every run).
        Needed: the role tiers (simulator, judge); where agent calls go (a downgrade saver's target, else the default
        tier); strong and cheap for a governor; for a Switchyard tier, also its weak/strong targets and judge. Savers
        come from the cell, else from the gateway config (the same precedence as `truecost serve`). Optional tiers
        (Nemotron, Switchyard) are only up while the cells that use them run, and the 27B is down during Nemotron."""
        from .config import Config

        cfg = Config.load(self.gw_config)
        cell = cell or {}
        specs = cell.get("savers") or [":".join([d["name"], ",".join(f"{k}={v}" for k, v in d.items() if k != "name")])
                                       for d in cfg.savers]
        need = set(cfg.roles.values())
        pinned = False
        for spec in specs:
            name, _, args = spec.partition(":")
            if name == "downgrade":
                kw = dict(a.split("=", 1) for a in args.split(",") if "=" in a)
                need.add(kw.get("to", "cheap"))
                pinned = True
        if not pinned:
            need.add(cfg.default_tier)
        if cell.get("governor") or cfg.governor:
            need |= {"strong", "cheap"}
        if any(cfg.upstreams[n].switchyard for n in need if n in cfg.upstreams):
            need |= {"strong", "cheap", *cfg.model_map.values()}
        bad = []
        for name in sorted(need & set(cfg.upstreams)):
            u = cfg.upstreams[name]
            root = u.base_url.rsplit("/v1", 1)[0]
            ok = False
            for path in ("/health", "/v1/health/ready"):  # SGLang/vLLM, then NVIDIA NIM
                try:
                    with urllib.request.urlopen(f"{root}{path}", timeout=10) as r:
                        ok = r.status == 200
                except OSError:
                    ok = False
                if ok:
                    break
            if not ok:
                bad.append(name)
        return bad

    def error_rate(self, run_id: str) -> tuple[int, int]:
        """Failed calls out of all calls, except one recognised agent-caused error: the agent's conversation outgrew
        the model's context window (a 400 the gateway passes through; a task failure, not infrastructure)."""
        from .ledger import Ledger

        r = Ledger(self.db).one("""SELECT COUNT(*) n, SUM(COALESCE(status, 0) != 200 AND NOT (COALESCE(status, 0) = 400
                                       AND (COALESCE(error, '') LIKE '%context length%'
                                            OR COALESCE(error, '') LIKE '%maximum context%'
                                            OR COALESCE(error, '') LIKE '%context_length_exceeded%'))) bad
                                   FROM calls WHERE run_id=?""", (run_id,))
        return (r["bad"] or 0), (r["n"] or 0)

    # ---- main loop ------------------------------------------------------------------------------
    def run(self, only: list[str] | None = None, seeds: list[int] | None = None) -> None:
        (self.out / "DONE").unlink(missing_ok=True)
        todo = [r for r in self.runs() if (not only or r[0]["name"] in only) and (not seeds or r[1] in seeds)]
        self.log(f"grid {self.name}: {len(todo)} runs")
        for cell, seed, run_id in todo:
            if self.state.get(run_id, {}).get("status") == "done":
                continue
            bad = self.upstreams_healthy(cell)
            if bad:
                self.log(f"STOPPED before {run_id}: upstream(s) down: {', '.join(bad)}")
                (self.out / "STOPPED").write_text(f"{time.strftime('%F %T')} upstream down: {bad}\n")
                return
            rundir = self.out / run_id
            if rundir.exists():
                import shutil

                shutil.rmtree(rundir)
            rundir.mkdir(parents=True, exist_ok=True)
            from .ledger import Ledger

            Ledger(self.db).purge_run(run_id)  # a re-run starts clean
            twin = Path(self.db).parent / "twins" / f"{run_id}.jsonl"
            twin.unlink(missing_ok=True)
            t0 = time.time()
            self.log(f"start {run_id}")
            gw = None
            try:
                gw = self.start_gateway(run_id, cell, rundir)
                cmd, cwd, env = self.workload_cmd(run_id, seed, rundir)
                (rundir / "cmd.json").write_text(json.dumps(cmd, indent=1))
                limit = float(self.wl.get("timeout_s", 5 * 3600))
                with open(rundir / "workload.log", "w") as log:
                    wp = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                          start_new_session=True)
                    timed_out = False
                    try:
                        rc = wp.wait(timeout=limit)
                    except subprocess.TimeoutExpired:  # a hung harness must not stall the grid
                        timed_out = True
                        os.killpg(wp.pid, signal.SIGTERM)
                        try:
                            wp.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            pass
                    try:  # children that outlived (or ignored) the leader's exit
                        os.killpg(wp.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    if timed_out:
                        raise RuntimeError(f"workload exceeded {limit / 3600:.1f} h and was stopped")
                self.stop_gateway(gw)
                gw = None
                if rc != 0:
                    raise RuntimeError(f"workload exited with code {rc}")
                self.import_results(run_id, rundir)
                from .ledger import Ledger

                n = Ledger(self.db).one("SELECT COUNT(DISTINCT task_key) n FROM tasks WHERE run_id=?", (run_id,))["n"]
                expected = self.expected_tasks()
                if expected and n < expected:
                    raise RuntimeError(f"only {n} of {expected} tasks graded: run incomplete")
                bad_calls, all_calls = self.error_rate(run_id)
                if all_calls == 0 or bad_calls / all_calls > 0.02:
                    raise RuntimeError(f"{bad_calls}/{all_calls} calls failed (> 2%): run not trusted")
                self.state[run_id] = {"status": "done", "rc": rc, "tasks": n, "wall_s": time.time() - t0,
                                      "failed_calls": bad_calls, "calls": all_calls}
                self.log(f"done {run_id}: {n} tasks graded in {(time.time() - t0) / 60:.1f} min (rc {rc}, "
                         f"{bad_calls}/{all_calls} failed calls)")
            except Exception as e:  # keep the grid moving
                self.state[run_id] = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
                self.log(f"FAILED {run_id}: {type(e).__name__}: {e}")
            finally:
                if gw is not None:
                    self.stop_gateway(gw)
                self.save()
        (self.out / "DONE").write_text(time.strftime("%F %T") + "\n")
        self.log(f"grid {self.name} finished")


def main(argv=None) -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="truecost crashtest")
    ap.add_argument("grid")
    ap.add_argument("--only", nargs="*", help="cell names to run")
    ap.add_argument("--seeds", nargs="*", type=int)
    a = ap.parse_args(argv)
    Grid(a.grid).run(a.only, a.seeds)


if __name__ == "__main__":
    main()
