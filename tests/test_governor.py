import json
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import make_client
from truecost.governor import compare, is_write

H = {"X-TrueCost-Task": "t1"}


def chat(c, text):
    return c.post("/v1/chat/completions", json={"model": "strong", "messages": [{"role": "user", "content": text}]}, headers=H)


def gate(tmp_path, thresholds):
    p = tmp_path / "gate.json"
    p.write_text(json.dumps({"features": ["lp_mean"], "coef": [5.0], "intercept": 0.0, "mean": [-0.5], "std": [0.5],
                             "thresholds": thresholds}))
    return str(p)


def test_record_mode_answers_with_strong_and_writes_twins(cfg, upstream):
    app, led = make_client(cfg, upstream, governor={"mode": "record"})
    with TestClient(app) as c:
        r = chat(c, "hello there")
    assert r.json()["choices"][0]["message"]["content"] == "answer from qwen3.8-27b"
    roles = {(x["role"], x["tier"]) for x in led.query("SELECT role, tier FROM calls")}
    assert roles == {("shadow", "cheap"), ("agent", "strong")}
    twins = [json.loads(l) for l in (Path(cfg.db).parent / "twins" / "r1.jsonl").read_text().splitlines()]
    assert len(twins) == 1 and twins[0]["kind"] == "text" and "lp_mean" in twins[0]["features"]


def test_cascade_accepts_confident_draft(cfg, upstream, tmp_path):
    app, led = make_client(cfg, upstream, governor={"mode": "cascade", "alpha": 0.05, "gate": gate(tmp_path, {"0.05": 0.5})})
    with TestClient(app) as c:
        r = chat(c, "hello there")
    assert r.json()["choices"][0]["message"]["content"] == "answer from qwen3.5-4b"
    assert "logprobs" not in r.json()["choices"][0]
    rows = led.query("SELECT tier, decision FROM calls")
    assert len(rows) == 1 and json.loads(rows[0]["decision"])["action"] == "accept"


def test_accepted_draft_streams_back_when_a_saver_buffers(cfg, upstream, tmp_path):
    # the hold saver buffers a streamed call so the governor can act on it; the accepted draft must still go back as SSE
    app, _ = make_client(cfg, upstream, savers=[{"name": "hold", "pattern": "update-bank"}],
                         governor={"mode": "cascade", "alpha": 0.05, "gate": gate(tmp_path, {"0.05": 0.5})})
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", headers=H, json={"model": "strong", "stream": True,
                                                            "messages": [{"role": "user", "content": "hello there"}]})
    assert r.headers["content-type"].startswith("text/event-stream")
    chunks = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: {")]
    assert chunks[0]["choices"][0]["delta"]["content"] == "answer from qwen3.5-4b"


def test_cascade_escalates_unsure_draft(cfg, upstream, tmp_path):
    upstream.logprob = -2.0
    app, led = make_client(cfg, upstream, governor={"mode": "cascade", "alpha": 0.05, "gate": gate(tmp_path, {"0.05": 0.5})})
    with TestClient(app) as c:
        r = chat(c, "hello there")
    assert r.json()["choices"][0]["message"]["content"] == "answer from qwen3.8-27b"
    acts = [json.loads(x["decision"])["action"] for x in led.query("SELECT decision FROM calls ORDER BY ts_start")]
    assert acts == ["draft-rejected", "escalate"]


def test_writes_are_pinned_to_strong(cfg, upstream, tmp_path):
    app, led = make_client(cfg, upstream, governor={"mode": "cascade", "alpha": 0.05, "gate": gate(tmp_path, {"0.05": 0.0})})
    with TestClient(app) as c:
        chat(c, "please cancel my order")
        chat(c, "look up my order")
    acts = [(x["tier"], json.loads(x["decision"])["action"]) for x in led.query("SELECT tier, decision FROM calls ORDER BY ts_start")]
    assert acts == [("cheap", "draft-pinned"), ("strong", "pin"), ("cheap", "accept")]


def test_naive_router_has_no_pins(cfg, upstream):
    app, led = make_client(cfg, upstream, governor={"mode": "naive", "threshold": 0.5})
    with TestClient(app) as c:
        chat(c, "please cancel my order")
    rows = led.query("SELECT tier, decision FROM calls")
    assert len(rows) == 1 and rows[0]["tier"] == "cheap"


def test_compare_and_is_write():
    tc = lambda n, a: {"tool_calls": [{"function": {"name": n, "arguments": json.dumps(a)}}]}
    assert compare(tc("get_order_details", {"order_id": "#W1"}), tc("get_order_details", {"order_id": " #W1"}))[0]
    assert not compare(tc("get_order_details", {"order_id": "#W1"}), tc("get_order_details", {"order_id": "#w1"}))[0]
    assert not compare(tc("get_order_details", {"order_id": "#W1"}), tc("get_order_details", {"order_id": "#W2"}))[0]
    assert not compare(tc("x", {"ids": ["b", "a"]}), tc("x", {"ids": ["a", "b"]}))[0]  # order kept (conservative)
    assert compare({"content": "Your order #W1 costs $25."}, {"content": "Order #W1 is $25."}, "")[0]
    assert not compare({"content": "Refund of 99 dollars."}, {"content": "Refund of 25 dollars."}, "")[0]
    assert not compare({"content": "Can you confirm?"}, {"content": "Done."}, "")[0]
    assert is_write("cancel_pending_order") and is_write("exchange_delivered_order_items")
    assert not is_write("get_order_details") and not is_write("find_user_id_by_email")
    assert is_write("api_fetch", {"method": "POST"}) and not is_write("api_fetch", {"method": "GET"})


def test_pins_check_every_tool_call(cfg, upstream, tmp_path):
    from truecost.governor import Governor
    g = Governor({"mode": "cascade", "alpha": 0.05, "gate": gate(tmp_path, {"0.05": 0.0})})
    multi = {"tool_calls": [{"function": {"name": "get_order_details", "arguments": "{}"}},
                            {"function": {"name": "cancel_pending_order", "arguments": "{}"}}]}
    assert g.pinned(multi) == "cancel_pending_order"
    read_only = {"tool_calls": [{"function": {"name": "get_order_details", "arguments": "{}"}}]}
    assert g.pinned(read_only) is None
    # twins compare the whole action set
    tc = lambda *names: {"tool_calls": [{"function": {"name": n, "arguments": "{}"}} for n in names]}
    assert not compare(tc("get_order_details", "cancel_pending_order"), tc("get_order_details"))[0]


def _post(c, messages):
    return c.post("/v1/chat/completions", json={"model": "strong", "messages": messages}, headers=H)


def _gov2(tmp_path, **kw):
    return {"mode": "cascade", "alpha": 0.05, "gate": gate(tmp_path, {"0.05": 0.0}), "policy": "retail", **kw}


def test_policy_check_reasks_a_write_that_skips_the_policy(cfg, upstream, tmp_path):
    # a cancel with no authentication, no "yes" and no order read: blocked; the re-asked strong model (the fake answers
    # the note's "look up" with a read) does a read instead, which passes
    app, led = make_client(cfg, upstream, governor=_gov2(tmp_path))
    with TestClient(app) as c:
        r = _post(c, [{"role": "system", "content": "policy"}, {"role": "user", "content": "please cancel my order"}])
    calls = r.json()["choices"][0]["message"]["tool_calls"]
    assert [x["function"]["name"] for x in calls] == ["get_order_details"]
    rows = [json.loads(x["decision"]) for x in led.query("SELECT decision FROM calls ORDER BY ts_start")]
    reask = [d for d in rows if d["action"] == "policy-reask"]
    assert len(reask) == 1 and reask[0]["blocked"] == "cancel_pending_order"
    assert set(reask[0]["rules"]) == {"auth_first", "confirm_first", "status_first"}
    sent = upstream.requests[-1]["body"]["messages"]
    assert sent[-1]["role"] == "system" and "POLICY CHECK" in sent[-1]["content"]


def test_policy_fallback_never_returns_the_write(cfg, upstream, tmp_path):
    app, led = make_client(cfg, upstream, governor=_gov2(tmp_path, policy_retries=0))
    with TestClient(app) as c:
        r = _post(c, [{"role": "user", "content": "please cancel my order"}])
    msg = r.json()["choices"][0]["message"]
    assert not msg.get("tool_calls") and "verify your identity" in msg["content"]
    fb = [json.loads(x["decision"]) for x in led.query("SELECT decision, cost_usd FROM calls WHERE tier = 'local'")]
    assert fb and fb[0]["action"] == "policy-fallback"


def test_policy_check_passes_a_compliant_write(cfg, upstream, tmp_path):
    convo = [
        {"role": "user", "content": "my email is a@b.com"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c0", "type": "function", "function": {
            "name": "find_user_id_by_email", "arguments": json.dumps({"email": "a@b.com"})}}]},
        {"role": "tool", "tool_call_id": "c0", "content": "u1"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c2", "type": "function", "function": {
            "name": "get_order_details", "arguments": json.dumps({"order_id": "#W1"})}}]},
        {"role": "tool", "tool_call_id": "c2", "content": json.dumps({"order_id": "#W1", "user_id": "u1", "status": "pending"})},
        {"role": "assistant", "content": "I will cancel order #W1 (no longer needed). Confirm?"},
        {"role": "user", "content": "yes, please cancel it"},
    ]
    app, led = make_client(cfg, upstream, governor=_gov2(tmp_path))
    with TestClient(app) as c:
        r = _post(c, convo)
    tc = r.json()["choices"][0]["message"]["tool_calls"][0]["function"]
    assert tc["name"] == "cancel_pending_order"
    acts = [json.loads(x["decision"])["action"] for x in led.query("SELECT decision FROM calls")]
    assert "policy-reask" not in acts and "policy-fallback" not in acts
