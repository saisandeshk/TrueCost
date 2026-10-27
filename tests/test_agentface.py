from truecost.agentface import burn_rate, format_line
from truecost.ledger import Ledger


def test_burn_rate_from_limit_readings(tmp_path):
    led = Ledger(tmp_path / "l.db")
    acct = "claude_code:.claude-x"
    for ts, pct in ((100, 10.0), (200, 12.0), (300, 15.0)):
        led.add_limit(acct, "seven_day", pct, 9999, ts)
    for i, (ts, usd) in enumerate(((150, 1.0), (250, 2.0))):
        led.add_call({"call_id": f"c{i}", "ts_start": ts, "cost_usd": usd, "meta": {"account": acct}})
    assert abs(burn_rate(led, acct, "seven_day") - 5.0 / 3.0) < 1e-9  # 5 % over $3
    led.add_limit(acct, "seven_day", 1.0, 20000, 400)                  # window reset: not a burn pair
    assert abs(burn_rate(led, acct, "seven_day") - 5.0 / 3.0) < 1e-9


def test_status_line_format():
    s = {"task": {"title": "fix the billing test", "usd": 0.42, "outcome": "verified", "week_pct": 2.1},
         "today": {"tasks": 7, "verified": 5, "usd_per_verified": 0.31, "wasted_share": 0.24},
         "limits": {"five_hour": {"used_pct": 12}, "seven_day": {"used_pct": 79}}}
    line = format_line(s)
    assert "≈2.1% of week" in line and "5/7 ✓" in line and "wk 79%" in line


def test_live_limits_from_statusline_payload(tmp_path):
    from truecost.agentface import record_live_limits
    from truecost.ledger import Ledger
    led = Ledger(str(tmp_path / "l.db"))
    p = {"transcript_path": str(tmp_path / ".claude-b/projects/x/s.jsonl"),
         "rate_limits": {"five_hour": {"used_percentage": 29, "resets_at": 100}, "seven_day": {"used_percentage": 20}}}
    assert record_live_limits(led, "claude", p) == 2
    assert record_live_limits(led, "claude", p) == 0          # unchanged readings are not re-booked
    p["rate_limits"]["five_hour"]["used_percentage"] = 31
    assert record_live_limits(led, "claude", p) == 1
    rows = led.query("SELECT source, window, used_pct FROM limits ORDER BY ts")
    assert rows[-1] == {"source": "claude_code:.claude-b", "window": "five_hour", "used_pct": 31.0}


def test_plan_delta_measures_usage_since_task_start(tmp_path):
    from truecost.agentface import plan_delta
    from truecost.ledger import Ledger
    led = Ledger(str(tmp_path / "l.db"))
    led.add_limit("codex", "five_hour", 10.0, 500.0, ts=100.0)
    led.add_limit("codex", "five_hour", 14.0, 500.0, ts=200.0)
    led.add_limit("codex", "five_hour", 17.0, 500.0, ts=300.0)
    assert plan_delta(led, "codex", "five_hour", 150.0) == 7.0
    assert plan_delta(led, "codex", "five_hour", 50.0) is None      # no reading before the task started
    led.add_limit("codex", "five_hour", 2.0, 900.0, ts=400.0)       # window reset: cannot difference
    assert plan_delta(led, "codex", "five_hour", 150.0) is None


def test_global_hook_is_silent_outside_allowed_projects(tmp_path, monkeypatch):
    from truecost.agentface import hook
    from truecost.ledger import Ledger
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    monkeypatch.setenv("TRUECOST_PROJECTS", str(proj))
    led = Ledger(str(tmp_path / "l.db"))
    assert hook("stop", "codex", {"cwd": str(tmp_path)}, led) is None
    assert hook("stop", "codex", {"cwd": str(proj / "sub")}, led)["systemMessage"].startswith("TrueCost")
