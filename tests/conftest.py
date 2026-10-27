import json

import httpx
import pytest

from truecost.config import Config
from truecost.energy import EnergyMeter
from truecost.gateway import Gateway, create_app
from truecost.ledger import Ledger


class FakeUpstream:
    """An OpenAI-compatible model server: records requests, answers with fixed usage."""

    def __init__(self):
        self.requests: list[dict] = []
        self.logprob = -0.05

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "body": body})
        url = str(request.url)
        if url.endswith("/messages"):
            usage = {"input_tokens": 100, "output_tokens": 7, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50}
            if body.get("stream"):
                evs = [{"type": "message_start", "message": {"usage": {"input_tokens": 100, "cache_read_input_tokens": 900,
                                                                       "cache_creation_input_tokens": 50, "output_tokens": 1}}},
                       {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "name": "Bash", "input": {}}},
                       {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
                       {"type": "message_stop"}]
                text = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in evs)
                return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={"type": "message", "role": "assistant", "model": body["model"], "stop_reason": "end_turn",
                                             "content": [{"type": "text", "text": "hi"}], "usage": usage})
        if url.endswith("/responses"):
            obj = {"id": "r1", "object": "response", "status": "completed", "model": body["model"],
                   "output": [{"type": "function_call", "name": "shell", "arguments": "{}"}],
                   "usage": {"input_tokens": 500, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 400},
                             "output_tokens_details": {"reasoning_tokens": 5}}}
            if body.get("stream"):
                text = f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': obj})}\n\n"
                return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json=obj)
        msgs = body.get("messages", [])
        prompt = sum(len(json.dumps(m)) for m in msgs) // 4
        usage = {"prompt_tokens": prompt, "completion_tokens": 10, "total_tokens": prompt + 10,
                 "prompt_tokens_details": {"cached_tokens": prompt // 2}}
        if "summary" in json.dumps(msgs[:1]).lower() and msgs and msgs[0].get("role") == "system" \
                and "compress" in msgs[0].get("content", ""):
            msg = {"role": "assistant", "content": "SUMMARY: user verified, order #W1"}
        else:
            msg = {"role": "assistant", "content": f"answer from {body['model']}"}
            last = json.dumps(msgs[-1:]).lower()
            if "please cancel" in last:
                msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "cancel_pending_order", "arguments": json.dumps({"order_id": "#W1", "reason": "no longer needed"})}}]}
            elif "look up" in last:
                msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "get_order_details", "arguments": json.dumps({"order_id": "#W1"})}}]}
        if body.get("stream"):
            chunks = [
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "hel"}}]},
                {"choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": usage},
            ]
            text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})
        if "too long" in json.dumps(msgs[-1:]) and (body.get("max_tokens") or 0) + 27079 > 32768:
            return httpx.Response(400, json={"object": "error", "message": "Requested token count exceeds the model's "
                                  "maximum context length of 32768 tokens. You requested a total of 33223 tokens: "
                                  "27079 tokens from the input messages and 6144 tokens for the completion."})
        if "bad request" in json.dumps(msgs[-1:]):
            return httpx.Response(400, json={"object": "error", "message": "invalid"})
        choice = {"index": 0, "finish_reason": "stop", "message": msg}
        if body.get("logprobs"):
            lp = self.logprob
            choice["logprobs"] = {"content": [{"token": t, "logprob": lp} for t in ("<tool_call>", "function", "=x", ">", "a", "</tool_call>")]}
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "model": body["model"],
                                         "choices": [choice], "usage": usage})


@pytest.fixture
def upstream():
    return FakeUpstream()


@pytest.fixture
def cfg(tmp_path):
    return Config.from_dict({
        "db": str(tmp_path / "ledger.db"), "run": "r1", "workload": "test",
        "upstreams": {
            "strong": {"base_url": "http://strong/v1", "model": "qwen3.8-27b", "gpu": 0},
            "cheap": {"base_url": "http://cheap/v1", "model": "qwen3.5-4b", "gpu": 1},
            "sim": {"base_url": "http://sim/v1", "model": "qwen3.5-9b", "gpu": 1},
        },
        "roles": {"simulator": "sim", "judge": "sim"},
        "model_map": {"claude-haiku*": "cheap", "claude-*": "strong"},
    })


def make_client(cfg, upstream, savers=None, governor=None):
    meter = EnergyMeter(gpus=[])
    gw_kwargs = {"meter": meter, "transport": httpx.MockTransport(upstream.handler)}
    if governor is not None:
        from truecost.governor import Governor
        from pathlib import Path
        gw_kwargs["governor"] = Governor(governor, run_id=cfg.run, out_dir=str(Path(cfg.db).parent / "twins"))
    if savers is not None:
        from truecost.savers import build
        gw_kwargs["savers"] = build(savers)
    app = create_app(cfg, **gw_kwargs)
    return app, Ledger(cfg.db)
