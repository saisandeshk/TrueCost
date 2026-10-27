import json

from fastapi.testclient import TestClient

from conftest import make_client
from truecost.console import create_console
from truecost.report import import_tau2


def test_console_endpoints(cfg, upstream, tmp_path):
    app, led = make_client(cfg, upstream)
    with TestClient(app) as c:  # a crash-test run (it has a condition) through the gateway
        for t in ("1", "2"):
            c.post("/v1/chat/completions", json={"model": "strong", "messages": [{"role": "user", "content": t}]},
                   headers={"X-TrueCost-Task": t})
    led.upsert_run({"run_id": "r1", "workload": "tau2-retail", "condition": "baseline"})
    p = tmp_path / "res.json"
    p.write_text(json.dumps({"simulations": [{"task_id": t, "trial": 0, "reward_info": {"reward": r}, "messages": []}
                                            for t, r in (("1", 1), ("2", 0))]}))
    import_tau2(led, str(p), "r1", "tau2-retail")
    led.upsert_task({"task_key": "cc:s:1", "run_id": "observer", "source": "claude_code", "outcome": "verified",
                     "started": __import__("time").time(), "title": "fix tests"})
    con = TestClient(create_console([cfg.db]))
    ov = con.get("/api/overview?range=all&source=all").json()
    assert ov["tasks"] == 1  # the crash-test run is excluded from usage
    assert con.get("/api/overview?range=all&source=crashtest").json()["tasks"] == 2
    ct = con.get("/api/crashtest?workload=tau2-retail").json()
    assert ct["baseline"] == "baseline" and ct["rows"][0]["stars"]["reliability"] == 5
    assert con.get("/api/task?key=r1/1").json()["task"]["outcome"] == "verified"
    assert con.get("/").status_code == 200 and con.get("/static/app.js").status_code == 200


def test_outcome_command_marks_tasks(tmp_path):
    from truecost import cli
    from truecost.ledger import Ledger

    db = str(tmp_path / "l.db")
    cli.main(["--db", db, "outcome", "r1", "t1", "verified"])
    cli.main(["--db", db, "outcome", "r1", "t2", "failed"])
    rows = {r["task_key"]: r["outcome"] for r in Ledger(db).query("SELECT task_key, outcome FROM tasks")}
    assert rows == {"r1/t1": "verified", "r1/t2": "failed"}


def test_board_cached_until_the_ledger_changes(tmp_path):
    from truecost.console import Console
    from truecost.ledger import Ledger

    db = str(tmp_path / "l.db")
    led = Ledger(db)
    con = Console([db])
    calls = []

    def compute():
        calls.append(1)
        return len(calls)

    assert con.cached(("k",), compute) == 1
    assert con.cached(("k",), compute) == 1  # unchanged ledger: reused
    led.upsert_run({"run_id": "r1", "workload": "tau2-retail", "condition": "baseline"})
    assert con.cached(("k",), compute) == 2  # a write recomputes


def test_warm_fills_the_boards_the_pages_ask_for(cfg, upstream, tmp_path):
    from truecost.console import Console

    _, led = make_client(cfg, upstream)
    led.upsert_run({"run_id": "r1", "workload": "tau2-retail", "condition": "baseline"})
    p = tmp_path / "res.json"
    p.write_text(json.dumps({"simulations": [{"task_id": "1", "trial": 0, "reward_info": {"reward": 1}, "messages": []}]}))
    import_tau2(led, str(p), "r1", "tau2-retail")
    con = Console([cfg.db])
    con.warm()
    assert {k for k in con._memo} == {("crashtest", "tau2-retail", None, None),
                                      ("crashtest", "tau2-retail", None, "gov"), ("attacks", "tau2-retail")}
    assert con.cached(("crashtest", "tau2-retail", None, None), lambda: 1 / 0)["baseline"] == "baseline"  # served warm
