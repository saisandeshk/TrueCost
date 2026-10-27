"""End to end: grid runner -> gateway subprocess -> fake upstream -> a fake agent (command workload) -> import."""
import json
import socket
import sys
import textwrap
import threading
import time

import uvicorn
from fastapi import FastAPI, Request

from truecost.crashtest import Grid
from truecost.ledger import Ledger


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def fake_upstream(port):
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        return {"id": "x", "object": "chat.completion", "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 40}}}

    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return srv
        except OSError:
            time.sleep(0.05)


AGENT = textwrap.dedent("""
    import json, sys, urllib.request
    gw, run, out = sys.argv[1:4]
    sims = []
    for t in ("1", "2", "3"):
        req = urllib.request.Request(gw + "/chat/completions", method="POST",
            data=json.dumps({"model": "strong", "messages": [{"role": "user", "content": t}]}).encode(),
            headers={"Content-Type": "application/json", "X-TrueCost-Task": t, "X-TrueCost-Run": run})
        urllib.request.urlopen(req).read()
        sims.append({"task_id": t, "trial": 0, "reward_info": {"reward": 1.0 if t != "3" else 0.0}, "messages": []})
    json.dump({"simulations": sims}, open(out, "w"))
""")


def test_grid_runs_command_workload(tmp_path):
    up_port, gw_port = free_port(), free_port()
    srv = fake_upstream(up_port)
    (tmp_path / "agent.py").write_text(AGENT)
    (tmp_path / "gw.toml").write_text(f'db = "{tmp_path}/l.db"\n[upstreams.strong]\n'
                                      f'base_url = "http://127.0.0.1:{up_port}/v1"\nmodel = "qwen3.8-27b"\n')
    (tmp_path / "grid.toml").write_text(textwrap.dedent(f"""
        [grid]
        name = "t"
        db = "{tmp_path}/l.db"
        gateway_config = "{tmp_path}/gw.toml"
        out = "{tmp_path}/out"
        python = "{sys.executable}"
        port = {gw_port}
        [workload]
        kind = "command"
        format = "tau2"
        workload_name = "fake-retail"
        cmd = ["{sys.executable}", "{tmp_path}/agent.py", "{{gateway}}", "{{run}}", "{{out}}"]
        [[cells]]
        name = "baseline"
        seeds = [300, 301]
    """))
    Grid(str(tmp_path / "grid.toml")).run()
    state = json.loads((tmp_path / "out" / "state.json").read_text())
    assert [state[r]["status"] for r in ("t-baseline-s300", "t-baseline-s301")] == ["done", "done"]
    led = Ledger(tmp_path / "l.db")
    assert led.one("SELECT COUNT(*) n FROM tasks WHERE outcome='verified'")["n"] == 4
    assert led.one("SELECT COUNT(*) n FROM calls WHERE task_key LIKE 't-baseline-s300/%'")["n"] == 3
    assert led.one("SELECT workload, condition FROM runs WHERE run_id='t-baseline-s300'") == \
        {"workload": "fake-retail", "condition": "baseline"}
    # a re-run purges and replaces, never duplicates
    state["t-baseline-s300"]["status"] = "failed"
    (tmp_path / "out" / "state.json").write_text(json.dumps(state))
    Grid(str(tmp_path / "grid.toml")).run()
    srv.should_exit = True
    assert led.one("SELECT COUNT(*) n FROM calls WHERE task_key LIKE 't-baseline-s300/%'")["n"] == 3
