from truecost import planmeter
from truecost.ledger import Ledger


def test_fit_recovers_per_model_meter_rates(tmp_path):
    """Two models metered at 2%/$ and 10%/$: one reading per run, arriving with (and including) its first call."""
    led = Ledger(str(tmp_path / "l.db"))
    rate = {"big": 2.0, "small": 10.0}
    used, t = 0.0, 1000.0
    calls = []
    for i in range(40):
        model = "big" if i % 2 else "small"
        usd = 1.0 if model == "big" else 0.3
        used += rate[model] * usd
        led.upsert_task({"task_key": f"r-{model}/ex{i}", "run_id": f"r-{model}", "task_id": f"ex{i}", "source": "claude",
                         "workload": "w", "title": "x", "started": t, "ended": t + 50, "outcome": "verified",
                         "meta": {"limits_after": {"five_hour": round(used), "five_hour_resets": 9e9}}}, merge=False)
        calls.append({"call_id": f"c{i}", "ts_start": t + 10, "ts_end": t + 20, "source": "claude_code",
                      "run_id": f"r-{model}", "task_key": f"r-{model}/ex{i}", "model": model, "cost_usd": usd,
                      "meta": {"subscription": True}})
        t += 100
    led.add_calls(calls)
    f = planmeter.fit(led, "claude", reps=50)
    k = {m["model"]: m["k"] for m in f["models"]}
    assert abs(k["big"] - 2.0) < 0.5 and abs(k["small"] - 10.0) < 2.5
    runs = {r["run_id"]: r for r in f["runs"]}
    assert abs(runs["r-big"]["plan_pct_per_pass"] - 2.0) < 0.5   # $1 per pass at ~2%/$


def test_models_seen_in_too_few_blocks_get_no_estimate(tmp_path):
    led = Ledger(str(tmp_path / "l.db"))
    calls = []
    for i in range(30):
        model = "rare" if i == 3 else "common"
        led.upsert_task({"task_key": f"r/{i}", "run_id": "r", "task_id": str(i), "source": "claude", "workload": "w",
                         "title": "x", "started": 100.0 * i, "ended": 100.0 * i + 50, "outcome": "verified",
                         "meta": {"limits_after": {"five_hour": i, "five_hour_resets": 9e9}}}, merge=False)
        calls.append({"call_id": f"c{i}", "ts_start": 100.0 * i + 10, "ts_end": 100.0 * i + 20, "source": "claude_code",
                      "run_id": "r", "task_key": f"r/{i}", "model": model, "cost_usd": 1.0, "meta": {"subscription": True}})
    led.add_calls(calls)
    f = planmeter.fit(led, "claude", reps=20, block=1)
    k = {m["model"]: m["k"] for m in f["models"]}
    assert k["rare"] is None and k["common"] is not None
    assert f["runs"][0]["plan_pct_per_pass"] is None   # a run with an unpriced model has no plan cost


def test_a_shared_accounts_concurrent_runs_do_not_leak_into_another_accounts_fit(tmp_path):
    """claude-b runs (clean meter) interleaved in time with a shared claude-a run: the claude-a calls must not be
    counted in claude-b's intervals, and claude-a's readings must not be used."""
    led = Ledger(str(tmp_path / "l.db"))
    rate = {"big": 2.0, "small": 10.0}
    used, t, calls = 0.0, 1000.0, []
    for i in range(40):
        model = "big" if i % 2 else "small"
        usd = 1.0 if model == "big" else 0.3
        used += rate[model] * usd
        led.upsert_task({"task_key": f"b-{model}/ex{i}", "run_id": f"b-{model}", "task_id": f"ex{i}", "source": "claude",
                         "workload": "w", "title": "x", "started": t, "ended": t + 50, "outcome": "verified",
                         "meta": {"limits_after": {"five_hour": round(used), "five_hour_resets": 9e9},
                                  "account": "claude_code:.claude-b"}}, merge=False)
        calls.append({"call_id": f"b{i}", "ts_start": t + 10, "ts_end": t + 20, "source": "claude_code",
                      "run_id": f"b-{model}", "task_key": f"b-{model}/ex{i}", "model": model, "cost_usd": usd,
                      "meta": {"subscription": True, "account": "claude_code:.claude-b"}})
        # the shared account works at the same time, with a big model, and its own (polluted) readings
        led.upsert_task({"task_key": f"a/ex{i}", "run_id": "a-big", "task_id": f"ex{i}", "source": "claude",
                         "workload": "w", "title": "x", "started": t + 30, "ended": t + 60, "outcome": "verified",
                         "meta": {"limits_after": {"five_hour": 3 * i, "five_hour_resets": 8e9},
                                  "account": "claude_code:.claude-a", "shared_account": True}}, merge=False)
        calls.append({"call_id": f"a{i}", "ts_start": t + 35, "ts_end": t + 40, "source": "claude_code",
                      "run_id": "a-big", "task_key": f"a/ex{i}", "model": "big", "cost_usd": 2.0,
                      "meta": {"subscription": True, "account": "claude_code:.claude-a"}})
        t += 100
    led.add_calls(calls)
    f = planmeter.fit(led, "claude", basis="usd", reps=50)
    k = {m["model"]: m["k"] for m in f["models"]}
    assert abs(k["big"] - 2.0) < 0.5 and abs(k["small"] - 10.0) < 2.5
