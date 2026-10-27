import pytest
from truecost.ledger import Ledger


@pytest.fixture
def ledger(tmp_path):
    db = Ledger(tmp_path / "ledger.db")
    for run, scale in (("run", 1), ("base", 2)):
        db.upsert_run({"run_id": run})
        for i, (outcome, cost, joules) in enumerate([
            ("verified", 3, 5400), ("verified", 3, 1800), ("failed", 6, 14400),
            ("running", 100, 99999), ("unverified", 100, 99999),
        ]):
            key = f"{run}/{i}"
            db.upsert_task({"run_id": run, "task_key": key, "task_id": str(i),
                            "title": "duplicate title" if i < 2 else None,
                            "outcome": outcome, "violations": i if i < 3 else 0})
            db.add_call({"call_id": f"{key}/agent", "run_id": run, "task_key": key,
                         "role": "agent", "tier": "strong", "cost_usd": scale * cost,
                         "energy_j": scale * joules, "input_tokens": 100, "output_tokens": 10})
        # Summariser + governor move task 0's cost to 6, energy to 3 Wh.
        for role, cost, joules in (("summariser", 1, 3600), ("governor", 2, 1800)):
            db.add_call({"call_id": f"{run}/{role}", "run_id": run, "task_key": f"{run}/0",
                         "role": role, "cost_usd": scale * cost, "energy_j": scale * joules})
        for role in ("user", "grader"):
            db.add_call({"call_id": f"{run}/{role}", "run_id": run, "task_key": f"{run}/0",
                         "role": role, "cost_usd": 1000, "energy_j": None})
    yield db
    db.conn.close()
