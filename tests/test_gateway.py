import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_client
from truecost import pricing
from truecost.energy import EnergyMeter
from truecost.report import compare, import_tau2, run_metrics

H = {"X-TrueCost-Task": "t1", "X-TrueCost-Role": "agent"}


def chat(client, messages, headers=H, **kw):
    return client.post("/v1/chat/completions", json={"model": "strong", "messages": messages, **kw}, headers=headers)


def test_pricing_resolves_aliases():
    assert pricing.resolve("claude-opus-5-5-20260901") == "anthropic/claude-opus-5.5"
    assert pricing.resolve("qwen3.5-4b") == "qwen/qwen3.5-4b"
    assert pricing.resolve("unknown-model") is None
    # 1M uncached input + 1M cached + 1M output on the 27B: 0.50 + 0.10 + 3.00
    assert pricing.cost_usd("qwen3.8-27b", 1_000_000, 1_000_000, 0, 1_000_000) == pytest.approx(3.6)
    # null cache price bills cache reads at the input price
    assert pricing.cost_usd("qwen3-coder-30b", 0, 1_000_000, 0, 0) == pytest.approx(0.07)


def test_passthrough_books_call(cfg, upstream):
    app, led = make_client(cfg, upstream)
    with TestClient(app) as c:
        r = chat(c, [{"role": "user", "content": "hi"}])
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "answer from qwen3.8-27b"
    row = led.one("SELECT * FROM calls")
    assert row["task_key"] == "r1/t1" and row["role"] == "agent" and row["tier"] == "strong"
    assert row["input_tokens"] + row["cache_read_tokens"] > 0 and row["output_tokens"] == 10
    assert row["cost_usd"] > 0
    assert upstream.requests[0]["url"] == "http://strong/v1/chat/completions"


def test_roles_route_to_sim_and_skip_savers(cfg, upstream):
    app, led = make_client(cfg, upstream, savers=[{"name": "downgrade"}])
    with TestClient(app) as c:
        chat(c, [{"role": "user", "content": "hi"}], headers={**H, "X-TrueCost-Role": "simulator"})
        chat(c, [{"role": "user", "content": "hi"}])
    tiers = [r["tier"] for r in led.query("SELECT role, tier FROM calls ORDER BY ts_start")]
    assert tiers == ["sim", "cheap"]


def test_mask_and_squeeze(cfg, upstream):
    long = "x" * 5000
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}] + \
           [{"role": "tool", "content": long, "tool_call_id": str(i)} for i in range(10)]
    app, led = make_client(cfg, upstream, savers=[{"name": "mask", "keep_last": 3}])
    with TestClient(app) as c:
        chat(c, msgs)
    sent = upstream.requests[-1]["body"]["messages"]
    assert sum(1 for m in sent if m.get("content") == long) == 3
    assert json.loads(led.one("SELECT saver FROM calls")["saver"])["chars_removed"] > 0

    app, led = make_client(cfg, upstream, savers=[{"name": "squeeze", "max_chars": 100}])
    with TestClient(app) as c:
        chat(c, msgs)
    sent = upstream.requests[-1]["body"]["messages"]
    assert all(len(m["content"]) < 200 for m in sent if m["role"] == "tool")


def test_cap_hands_off_after_budget(cfg, upstream):
    tools = [{"type": "function", "function": {"name": "transfer_to_human_agents", "parameters": {}}}]
    app, led = make_client(cfg, upstream, savers=[{"name": "cap", "max_steps": 2}])
    with TestClient(app) as c:
        for _ in range(2):
            chat(c, [{"role": "user", "content": "hi"}], tools=tools)
        r = chat(c, [{"role": "user", "content": "hi"}], tools=tools)
    msg = r.json()["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "transfer_to_human_agents"
    assert len(upstream.requests) == 2
    assert led.one("SELECT COUNT(*) n FROM calls WHERE tier='local'")["n"] == 1


def test_summarise_folds_history_and_books_summary(cfg, upstream):
    msgs = [{"role": "system", "content": "policy"}] + \
           [{"role": "user" if i % 2 == 0 else "assistant", "content": "word " * 400} for i in range(20)]
    app, led = make_client(cfg, upstream, savers=[{"name": "summarise", "trigger_tokens": 3000, "keep_last": 4}])
    with TestClient(app) as c:
        chat(c, msgs)
        chat(c, msgs + [{"role": "user", "content": "more"}])
    agent_reqs = [r for r in upstream.requests if r["url"].startswith("http://strong")]
    first = agent_reqs[0]["body"]["messages"]
    assert "Conversation so far (summary)" in first[0]["content"] and len(first) == 5
    roles = [r["role"] for r in led.query("SELECT role FROM calls ORDER BY ts_start")]
    assert roles.count("summariser") == 1  # second call reuses the summary
    assert all(r["task_key"] == "r1/t1" for r in led.query("SELECT task_key FROM calls"))


def test_streaming_relays_and_books_usage(cfg, upstream):
    app, led = make_client(cfg, upstream)
    with TestClient(app) as c:
        r = chat(c, [{"role": "user", "content": "hi"}], stream=True)
        assert "hel" in r.text and "[DONE]" in r.text
    row = led.one("SELECT * FROM calls")
    assert row["stream"] == 1 and row["output_tokens"] == 10 and row["finish_reason"] == "stop"


def test_auto_task_key_groups_by_opening(cfg, upstream):
    app, led = make_client(cfg, upstream)
    with TestClient(app) as c:
        base = [{"role": "system", "content": "s"}, {"role": "user", "content": "order help"}]
        chat(c, base, headers={})
        chat(c, base + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "more"}], headers={})
    keys = {r["task_key"] for r in led.query("SELECT task_key FROM calls")}
    assert len(keys) == 1 and next(iter(keys)).startswith("r1/auto-")


def test_energy_time_share():
    m = EnergyMeter(gpus=[])
    m._handles = {0: None}
    readings = iter([0.0, 10.0, 30.0, 40.0, 70.0])
    m._read = lambda gpu: next(readings)
    m._last = {0: m._read(0)}       # 0
    m.begin("a", 0)                 # +10 idle
    m.begin("b", 0)                 # +20 -> a
    assert m.end("a", 0) == pytest.approx(25.0)   # +10 split a/b
    assert m.end("b", 0) == pytest.approx(5.0 + 30.0)
    assert m.idle_j[0] == pytest.approx(10.0)
    assert m.total_j[0] == pytest.approx(70.0)


def test_report_and_compare(cfg, upstream, tmp_path):
    app, led = make_client(cfg, upstream)
    with TestClient(app) as c:
        for run in ("base", "trt"):
            for t in ("1", "2", "3", "4"):
                chat(c, [{"role": "user", "content": f"task {t}"}],
                     headers={"X-TrueCost-Task": t, "X-TrueCost-Run": run})
    for run, rewards in (("base", [1, 1, 1, 0]), ("trt", [1, 0, 0, 0])):
        p = tmp_path / f"{run}.json"
        p.write_text(json.dumps({"simulations": [
            {"task_id": t, "trial": 0, "reward_info": {"reward": r, "db_check": {"db_match": bool(r)}}}
            for t, r in zip(("1", "2", "3", "4"), rewards)]}))
        assert import_tau2(led, str(p), run) == 4
    m = run_metrics(led, "base")
    assert m["verified"] == 3 and m["unmatched_calls"] == 0
    assert m["usd_per_verified"] == pytest.approx(m["usd_total"] / 3)
    cmp = compare(led, "base", "trt", reps=200)
    assert cmp["lost"] == 2 and cmp["gained"] == 0 and cmp["usd_per_verified_ratio"] > 2


def test_breaker_stops_loops_and_clips(cfg, upstream):
    tools = [{"type": "function", "function": {"name": "transfer_to_human_agents", "parameters": {}}}]
    call = {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function",
            "function": {"name": "get_page", "arguments": "{\"p\": 2}"}}]}
    loop = [{"role": "user", "content": "x"}] + [call, {"role": "tool", "content": "more pages", "tool_call_id": "1"}] * 3
    app, led = make_client(cfg, upstream, savers=[{"name": "breaker", "repeats": 3, "max_tool_chars": 50}])
    with TestClient(app) as c:
        r = chat(c, loop, tools=tools)
        assert r.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "transfer_to_human_agents"
        chat(c, [{"role": "user", "content": "y"}, {"role": "tool", "content": "z" * 500, "tool_call_id": "2"}],
             headers={"X-TrueCost-Task": "t2"})
    sent = upstream.requests[-1]["body"]["messages"][1]["content"]
    assert len(sent) < 200 and "clipped by the cost guard" in sent


def test_anthropic_messages_passthrough(cfg, upstream):
    app, led = make_client(cfg, upstream)
    with TestClient(app) as c:
        r = c.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 10,
                                         "messages": [{"role": "user", "content": "hi"}]}, headers=H)
        assert r.json()["content"][0]["text"] == "hi"
        r = c.post("/v1/messages", json={"model": "claude-haiku-5-5", "stream": True, "max_tokens": 10,
                                         "messages": [{"role": "user", "content": "hi"}]}, headers=H)
        assert "message_stop" in r.text
    rows = led.query("SELECT * FROM calls ORDER BY ts_start")
    assert [x["tier"] for x in rows] == ["strong", "cheap"]
    assert rows[0]["input_tokens"] == 100 and rows[0]["cache_read_tokens"] == 900 and rows[0]["cache_write_tokens"] == 50
    assert rows[1]["output_tokens"] == 7 and json.loads(rows[1]["tool_names"]) == ["Bash"]


def test_responses_passthrough(cfg, upstream):
    app, led = make_client(cfg, upstream)
    with TestClient(app) as c:
        c.post("/v1/responses", json={"model": "strong", "input": [{"role": "user", "content": "hi"}]}, headers=H)
        c.post("/v1/responses", json={"model": "strong", "stream": True, "input": "hi"}, headers=H)
    rows = led.query("SELECT * FROM calls ORDER BY ts_start")
    assert len(rows) == 2
    for x in rows:
        assert x["input_tokens"] == 100 and x["cache_read_tokens"] == 400 and x["output_tokens"] == 20
        assert json.loads(x["tool_names"]) == ["shell"]


def test_strip_grammar_removes_strict_and_response_format(cfg, upstream):
    cfg.upstreams["strong"].strip_grammar = True
    app, led = make_client(cfg, upstream)
    with TestClient(app) as c:
        chat(c, [{"role": "user", "content": "hi"}], response_format={"type": "json_object"},
             tools=[{"type": "function", "function": {"name": "f", "strict": True, "parameters": {}}}])
    sent = upstream.requests[-1]["body"]
    assert "response_format" not in sent and "strict" not in sent["tools"][0]["function"]


def test_max_tokens_shrinks_to_fit_context_and_4xx_passes_through(cfg, upstream):
    cfg.upstreams["strong"].max_tokens_cap = 6144
    app, led = make_client(cfg, upstream)
    with TestClient(app) as c:
        r = chat(c, [{"role": "user", "content": "this is too long"}])
        assert r.status_code == 200
        assert upstream.requests[-1]["body"]["max_tokens"] == 32768 - 27079 - 64
        r = chat(c, [{"role": "user", "content": "bad request"}])
        assert r.status_code == 400  # not 502: clients must not retry a deterministic error


def test_unmetered_energy_is_unknown_not_zero(cfg, upstream, tmp_path):
    app, led = make_client(cfg, upstream)
    with TestClient(app) as c:
        chat(c, [{"role": "user", "content": "x"}], headers={"X-TrueCost-Task": "1", "X-TrueCost-Run": "u"})
    p = tmp_path / "u.json"
    p.write_text(json.dumps({"simulations": [{"task_id": "1", "trial": 0, "reward_info": {"reward": 1.0}}]}))
    import_tau2(led, str(p), "u")
    m = run_metrics(led, "u")
    assert m["energy_wh_per_verified"] is None and m["usd_per_verified"] > 0


def test_passthrough_caps_output_tokens(cfg, upstream):
    """Claude Code asks for 128k output tokens; a local tier with a cap must not see more than the cap."""
    cfg.upstreams["strong"].max_tokens_cap = 6144
    app, _ = make_client(cfg, upstream)
    with TestClient(app) as c:
        c.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 128000,
                                     "messages": [{"role": "user", "content": "hi"}]}, headers=H)
        assert upstream.requests[-1]["body"]["max_tokens"] == 6144


def test_paged_runaway_reports_increasing_pages():
    import asyncio

    from truecost.savers import InjectRunaway

    def call(i, name="get_order_details", args='{"order_id": "#W1"}'):
        return [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function",
                                                                    "function": {"name": name, "arguments": args}}]},
                {"role": "tool", "tool_call_id": f"c{i}", "content": "{}"}]

    msgs = [{"role": "user", "content": "hi"}] + call(1) + call(2) + call(3, args='{"order_id": "#W2"}')

    class Ctx:
        body = {"messages": msgs}
        notes: dict = {}

    ctx = Ctx()
    asyncio.run(InjectRunaway(mode="paged", pages=40).before(ctx))
    tools = [m["content"] for m in ctx.body["messages"] if m["role"] == "tool"]
    assert "Page 1 of 40" in tools[0] and "Page 2 of 40" in tools[1] and "load page 3" in tools[1]
    assert "Page 1 of 40" in tools[2]   # a different call starts its own count
    assert ctx.notes["attack"] == "runaway-paged"


def test_switchyard_mode_books_inner_calls_once(cfg, upstream):
    """Switchyard routes the agent's call and makes its own model calls back through the gateway, forwarding the
    caller's bearer. Inner calls land on the agent's task with their real tier; the router's judge counts as agent-side
    overhead; the outer (envelope) call is not counted twice, and inner calls skip the savers."""
    from truecost.config import Upstream
    from truecost.gateway import decode_inner_token
    from truecost.report import per_task

    cfg.upstreams["switchyard"] = Upstream(name="switchyard", base_url="http://sy/v1", model="agent", switchyard=True)
    cfg.upstreams["judge"] = Upstream(name="judge", base_url="http://sim/v1", model="qwen3.5-9b", gpu=1,
                                      book_role="router")
    cfg.model_map["switchyard-judge"] = "judge"
    app, led = make_client(cfg, upstream, savers=[{"name": "downgrade", "to": "switchyard"}])
    seen = {}
    with TestClient(app) as c:
        inner = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")

        async def fake_switchyard(request):
            seen["auth"], seen["session"] = request.headers["authorization"], request.headers["x-switchyard-session-id"]
            body = json.loads(request.content)
            fwd = {"Authorization": request.headers["authorization"]}
            weak = (await inner.post("/v1/chat/completions", json={**body, "model": "qwen3.5-4b"}, headers=fwd)).json()
            await inner.post("/v1/chat/completions", json={"model": "switchyard-judge", "messages": body["messages"]},
                             headers=fwd)
            return httpx.Response(200, json={**weak, "model": "agent"})

        async def route(request):
            return await fake_switchyard(request) if request.url.host == "sy" else upstream.handler(request)

        app.state.gw.client = httpx.AsyncClient(transport=httpx.MockTransport(route))
        r = chat(c, [{"role": "user", "content": "hi"}])
        assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "answer from qwen3.5-4b"
    assert decode_inner_token(seen["auth"]) == {"r": "r1", "t": "r1/t1", "s": None} and seen["session"] == "r1/t1"
    rows = led.query("SELECT role, tier, task_key, energy_j FROM calls ORDER BY ts_start")
    assert sorted((x["role"], x["tier"]) for x in rows) == [("agent", "cheap"), ("envelope", "switchyard"),
                                                            ("router", "judge")]
    assert {x["task_key"] for x in rows} == {"r1/t1"}
    led.upsert_task({"task_key": "r1/t1", "run_id": "r1", "task_id": "t1", "outcome": "verified"})
    t = per_task(led, "r1")["r1/t1"]
    assert t["calls"] == 2 and t["cheap_calls"] == 1  # envelope excluded; judge counted as router overhead
