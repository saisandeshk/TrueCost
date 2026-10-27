"""The TrueCost gateway: an OpenAI-compatible endpoint that sits between an agent and its models.

Agents change one base URL and (optionally) send headers that tie each call to a task:
    X-TrueCost-Task     task id (calls without it are grouped by their opening messages)
    X-TrueCost-Run      run id (defaults to the gateway's configured run)
    X-TrueCost-Role     agent | simulator | judge | ... (default agent; only agent calls get savers)
    X-TrueCost-Session  optional session id

For every call the gateway applies the configured savers (the tricks under test) and, later, the
governor; forwards the request to the chosen tier; and writes a ledger row with tokens, list-price
cost and measured GPU energy.

Switchyard mode. An upstream with `switchyard = true` is NVIDIA NeMo Switchyard, a router that makes its own
model calls (the weak and strong tiers, and its judge). Switchyard's clients point back at this gateway with
`forward_auth = true`, so the caller's bearer token reaches us again on every inner call. The outer call carries
a bearer that encodes run, task and session ("tc1.<base64 json>"), plus Switchyard's session header, so each inner
call is booked to the right task with measured energy. The outer call itself is booked as role "envelope"
(no GPU, excluded from agent-side cost) so nothing is counted twice; inner calls skip savers and the governor.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import pricing
from .config import Config, Upstream
from .energy import EnergyMeter
from .ledger import Ledger
from .savers import Saver, build as build_savers

HEADER_TASK, HEADER_RUN, HEADER_ROLE, HEADER_SESSION = (
    "x-truecost-task", "x-truecost-run", "x-truecost-role", "x-truecost-session")
SWITCHYARD_SESSION = "x-switchyard-session-id"
INNER, ENVELOPE = "inner", "envelope"  # Switchyard mode: calls made by the router, and the agent's call to it


def encode_inner_token(run_id: str, task_key: str, session_id: str | None) -> str:
    raw = json.dumps({"r": run_id, "t": task_key, "s": session_id}, separators=(",", ":")).encode()
    return "tc1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_inner_token(auth: str | None) -> dict | None:
    tok = (auth or "").removeprefix("Bearer ").strip()
    if not tok.startswith("tc1."):
        return None
    try:
        b = tok[4:]
        return json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None


def _auto_task_key(body: dict) -> str:
    """Group header-less calls by their opening messages (system prompt + first user turn)."""
    msgs = body.get("messages") or []
    head = [m for m in msgs if m.get("role") == "system"][:1] + [m for m in msgs if m.get("role") == "user"][:1]
    h = hashlib.sha1(json.dumps(head, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return f"auto-{h}"


class CallContext:
    """Everything a saver or the governor may look at or change for one call."""

    def __init__(self, gw: "Gateway", body: dict, tier: str, role: str, run_id: str, task_key: str,
                 session_id: str | None):
        self.gw, self.body, self.tier, self.role = gw, body, tier, role
        self.run_id, self.task_key, self.session_id = run_id, task_key, session_id
        self.notes: dict[str, Any] = {}
        self.decision: dict[str, Any] | None = None
        self.reply: dict | None = None  # set to answer without calling a model
        self.reply_booked = False       # the reply was already booked (e.g. an accepted governor draft)
        self.governor_twin: dict | None = None
        self.upstream_status: int | None = None
        self.step = gw.next_step(task_key) if role == "agent" else None

    def state(self, name: str) -> dict:
        return self.gw.task_state(self.task_key, name)

    async def complete_internal(self, tier: str, messages: list[dict], role: str, **params) -> str:
        """A model call made by TrueCost itself (summaries, governor); booked to the same task."""
        body = {"messages": messages, "temperature": 0.0, **params}
        data = await self.gw.forward_json(body, tier, role=role, run_id=self.run_id, task_key=self.task_key,
                                          session_id=self.session_id)
        try:
            return data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError):
            return ""


class Gateway:
    def __init__(self, cfg: Config, ledger: Ledger | None = None, meter: EnergyMeter | None = None,
                 savers: list[Saver] | None = None, governor: Any = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.ledger = ledger or Ledger(cfg.db)
        self.meter = meter if meter is not None else EnergyMeter(cfg.nvml_gpus if cfg.nvml else [])
        self.savers = savers if savers is not None else build_savers(cfg.savers)
        if governor is None and cfg.governor:
            from pathlib import Path

            from .governor import Governor

            governor = Governor(cfg.governor, run_id=cfg.run, out_dir=str(Path(cfg.db).expanduser().parent / "twins"))
        self.governor = governor
        self._steps: dict[str, int] = {}
        self._pending: set[asyncio.Task] = set()  # booking finalizers that must outlive a cancelled request
        self._state: dict[tuple[str, str], dict] = {}
        self.client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(cfg.timeout_s, connect=30.0),
                                        limits=httpx.Limits(max_connections=512, max_keepalive_connections=128))

    # ---- per-task bookkeeping -------------------------------------------------------------------
    def next_step(self, task_key: str) -> int:
        self._steps[task_key] = self._steps.get(task_key, 0) + 1
        return self._steps[task_key]

    def task_state(self, task_key: str, name: str) -> dict:
        return self._state.setdefault((task_key, name), {})

    def meta_from(self, request: Request, body: dict) -> tuple[str, str, str, str | None]:
        h = request.headers
        inner = decode_inner_token(h.get("authorization"))
        if inner:  # a call Switchyard makes on the agent's behalf (see "Switchyard mode")
            return INNER, inner["r"], inner["t"], inner.get("s")
        role = (h.get(HEADER_ROLE) or "agent").lower()
        run_id = h.get(HEADER_RUN) or self.cfg.run or "default"
        task = h.get(HEADER_TASK)
        task_key = f"{run_id}/{task}" if task else f"{run_id}/{_auto_task_key(body)}"
        return role, run_id, task_key, h.get(HEADER_SESSION)

    # ---- upstream calls -------------------------------------------------------------------------
    def _prepare(self, body: dict, up: Upstream) -> dict:
        out = {**up.extra_body, **body, **up.force, "model": up.model}
        ctk = {**up.extra_body.get("chat_template_kwargs", {}), **(body.get("chat_template_kwargs") or {}),
               **up.force.get("chat_template_kwargs", {})}
        if ctk:
            out["chat_template_kwargs"] = ctk
        if up.strip_grammar:
            # On some GPU drivers SGLang crashes when a grammar is active (its token sync calls NCCL, which loads a CUDA
            # runtime the driver cannot run). Strict tool schemas and response_format switch grammars on, so they
            # are removed for local tiers; the change applies to every condition alike.
            out.pop("response_format", None)
            if out.get("tools"):
                out["tools"] = [{**t, "function": {k: v for k, v in (t.get("function") or {}).items() if k != "strict"}}
                                if isinstance(t, dict) and "function" in t else t for t in out["tools"]]
        if up.max_tokens_cap:
            for k in ("max_tokens", "max_completion_tokens"):
                if k in out:
                    out[k] = min(out[k] or up.max_tokens_cap, up.max_tokens_cap)
            if "max_tokens" not in out and "max_completion_tokens" not in out:
                out["max_tokens"] = up.max_tokens_cap
        return out

    def _headers(self, up: Upstream, ctx: "CallContext | None") -> dict:
        if up.switchyard and ctx is not None:
            return {"Authorization": f"Bearer {encode_inner_token(ctx.run_id, ctx.task_key, ctx.session_id)}",
                    SWITCHYARD_SESSION: ctx.task_key}
        return {"Authorization": f"Bearer {up.api_key}"}

    async def call_raw(self, body: dict, tier: str, ctx: "CallContext | None" = None) -> dict:
        """Non-streaming call to an upstream tier, not yet booked. Returns timing, energy and the JSON
        (an OpenAI-style error object on failure). If the server rejects the call because prompt + max_tokens
        exceeds its context window, it is retried once with max_tokens shrunk to what is left (>= 256)."""
        up = self.cfg.upstreams[tier]
        req = self._prepare(body, up)
        raw = await self._post(req, up, ctx)
        fit = _fit_max_tokens(raw, req)
        if fit is not None:
            raw2 = await self._post({**req, "max_tokens": fit}, up, ctx)
            raw2["energy"] = (raw2["energy"] or 0.0) + (raw["energy"] or 0.0)
            raw2["t0"] = raw["t0"]
            raw = raw2
        return raw

    async def _post(self, req: dict, up: Upstream, ctx: "CallContext | None" = None) -> dict:
        call_id = uuid.uuid4().hex
        t0 = time.time()
        self.meter.begin(call_id, up.gpu)
        status, data, err = 0, None, None
        try:
            r = await self.client.post(f"{up.base_url}/chat/completions", json=req,
                                       headers=self._headers(up, ctx))
            status = r.status_code
            try:
                data = r.json()
            except ValueError:
                data = {"error": {"message": r.text[:2000]}}
            if status >= 400:
                err = json.dumps(data)[:2000]
                if "error" not in data:
                    data = {"error": {"message": err}}
        except httpx.HTTPError as e:
            status, err = 599, f"{type(e).__name__}: {e}"
            data = {"error": {"message": err, "type": "upstream_unreachable"}}
        finally:
            energy = self.meter.end(call_id, up.gpu)
        return {"call_id": call_id, "t0": t0, "t1": time.time(), "req": req, "data": data, "status": status,
                "err": err, "energy": energy}



    async def book_raw(self, raw: dict, tier: str, role: str, ctx: "CallContext", decision: dict | None = None):
        await self._book(raw["call_id"], raw["t0"], raw["t1"], self.cfg.upstreams[tier], tier, role, ctx.run_id,
                         ctx.task_key, ctx.session_id, raw["req"], raw["data"], raw["status"], raw["err"],
                         raw["energy"], stream=False, ctx=ctx, decision=decision)

    async def forward_json(self, body: dict, tier: str, role: str, run_id: str, task_key: str,
                           session_id: str | None = None, ctx: CallContext | None = None) -> dict:
        """Non-streaming call to an upstream tier, booked to the ledger."""
        raw = await self.call_raw(body, tier, ctx)
        await self._book(raw["call_id"], raw["t0"], raw["t1"], self.cfg.upstreams[tier], tier, role, run_id, task_key,
                         session_id, raw["req"], raw["data"], raw["status"], raw["err"], raw["energy"], stream=False,
                         ctx=ctx)
        if ctx is not None:
            ctx.upstream_status = raw["status"]
        return raw["data"]

    async def _book(self, call_id, t0, t1, up: Upstream, tier, role, run_id, task_key, session_id, req,
                    data, status, err, energy, stream, ctx: CallContext | None = None, ttft=None,
                    decision: dict | None = None):
        data = data or {}
        if up.switchyard:  # the agent's call to the router: its inner calls carry the cost and energy
            role = ENVELOPE
        elif role == INNER:
            role = up.book_role or "agent"
        u = data.get("usage") or {}
        unknown = not u and err is not None and stream  # an interrupted stream: usage never arrived
        prompt = u.get("prompt_tokens") or 0
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        out = u.get("completion_tokens") or 0
        reasoning = (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tools = [t.get("function", {}).get("name") for t in msg.get("tool_calls") or []]
        row = {
            "call_id": call_id, "ts_start": t0, "ts_end": t1, "source": self.cfg.source, "run_id": run_id,
            "task_key": task_key, "session_id": session_id, "role": role, "tier": tier, "model": up.model,
            "api": "chat", "stream": int(stream), "input_tokens": max(0, prompt - cached),
            "cache_read_tokens": cached, "cache_write_tokens": 0, "output_tokens": out,
            "reasoning_tokens": reasoning, "n_messages": len(req.get("messages") or []),
            "n_tool_calls": len(tools), "tool_names": tools, "finish_reason": choice.get("finish_reason"),
            "status": status, "error": err, "latency_ms": (t1 - t0) * 1000.0,
            "ttft_ms": ttft, "gpu": up.gpu, "energy_j": energy,
            "cost_usd": None if unknown else pricing.cost_usd(up.price_model, max(0, prompt - cached), cached, 0, out),
            "saver": ctx.notes if ctx and ctx.notes else None,
            "decision": decision if decision is not None else (ctx.decision if ctx else None),
            "meta": {"step": ctx.step} if ctx and ctx.step is not None else None,
        }
        await asyncio.to_thread(self.ledger.add_call, row)

    # ---- request handling -----------------------------------------------------------------------
    async def chat(self, request: Request) -> Response:
        body = await request.json()
        role, run_id, task_key, session_id = self.meta_from(request, body)
        tier = self.cfg.tier_for(body.get("model"), role)
        ctx = CallContext(self, copy.deepcopy(body), tier, role, run_id, task_key, session_id)
        # a saver that must see the whole reply first (e.g. a hold on sensitive actions): call without streaming,
        # then replay the (possibly changed) reply to the agent as a stream
        buffered = bool(body.get("stream")) and role == "agent" and any(
            getattr(s, "needs_reply", False) and role in s.roles for s in self.savers)
        if buffered:
            ctx.body["stream"] = False
            ctx.body.pop("stream_options", None)
        if role == "agent":
            for s in self.savers:
                if role in s.roles:
                    await s.before(ctx)
                    if ctx.reply is not None:
                        break
            if ctx.reply is None and self.governor is not None:
                await self.governor.before(ctx)
        if ctx.reply is not None and ctx.reply_booked:  # an accepted governor draft: budgets still count it
            for sv in self.savers:
                if role in sv.roles:
                    await sv.after(ctx, ctx.reply)
            if buffered:
                return StreamingResponse(_as_sse(ctx.reply), media_type="text/event-stream")
            return JSONResponse(ctx.reply)
        if ctx.reply is not None:
            return await self._local_reply(ctx, body)
        if ctx.tier not in self.cfg.upstreams:
            return JSONResponse({"error": {"message": f"unknown tier {ctx.tier!r}"}}, status_code=400)
        if ctx.body.get("stream"):
            return await self._stream(ctx)
        data = await self.forward_json(ctx.body, ctx.tier, role, run_id, task_key, session_id, ctx=ctx)
        if "error" not in data:
            for s in self.savers:
                if role in s.roles:
                    await s.after(ctx, data)
            if self.governor is not None and role == "agent":
                await self.governor.after(ctx, data)
        if "error" not in data:
            if buffered:
                return StreamingResponse(_as_sse(data), media_type="text/event-stream")
            return JSONResponse(data)
        # pass client errors through as client errors, so agents do not retry deterministic failures
        up_status = ctx.upstream_status or 502
        return JSONResponse(data, status_code=up_status if 400 <= up_status < 500 else 502)

    async def _local_reply(self, ctx: CallContext, body: dict) -> Response:
        """Answer without a model call (e.g. a budget cap). Booked as a zero-cost call."""
        data = {"id": f"truecost-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(time.time()),
                "model": "truecost", **ctx.reply,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
        up = self.cfg.upstreams.get(ctx.tier) or next(iter(self.cfg.upstreams.values()))
        now = time.time()
        await self._book(uuid.uuid4().hex, now, now, up, "local", ctx.role, ctx.run_id, ctx.task_key,
                         ctx.session_id, ctx.body, data, 200, None, 0.0, stream=False, ctx=ctx)
        if body.get("stream"):
            return StreamingResponse(_as_sse(data), media_type="text/event-stream")
        return JSONResponse(data)

    async def book_local(self, ctx: CallContext, data: dict, decision: dict) -> None:
        """Book a reply TrueCost wrote itself (no model call: zero cost and energy) on the task's receipt."""
        up = self.cfg.upstreams.get(ctx.tier) or next(iter(self.cfg.upstreams.values()))
        now = time.time()
        await self._book(uuid.uuid4().hex, now, now, up, "local", ctx.role, ctx.run_id, ctx.task_key, ctx.session_id,
                         ctx.body, {**data, "usage": {}}, 200, None, 0.0, stream=False, ctx=ctx, decision=decision)

    async def _stream(self, ctx: CallContext) -> Response:
        up = self.cfg.upstreams[ctx.tier]
        req = self._prepare(ctx.body, up)
        req["stream_options"] = {**(req.get("stream_options") or {}), "include_usage": True}
        call_id, t0 = uuid.uuid4().hex, time.time()
        self.meter.begin(call_id, up.gpu)
        upstream_req = self.client.build_request("POST", f"{up.base_url}/chat/completions", json=req,
                                                 headers=self._headers(up, ctx))
        try:
            r = await self.client.send(upstream_req, stream=True)
        except httpx.HTTPError as e:
            energy = self.meter.end(call_id, up.gpu)
            err = f"{type(e).__name__}: {e}"
            await self._book(call_id, t0, time.time(), up, ctx.tier, ctx.role, ctx.run_id, ctx.task_key,
                             ctx.session_id, req, None, 599, err, energy, stream=True, ctx=ctx)
            return JSONResponse({"error": {"message": err}}, status_code=502)

        acc = {"usage": None, "finish": None, "tools": [], "ttft": None}

        async def relay():
            buf, err, head = b"", None, b""
            try:
                async for chunk in r.aiter_bytes():
                    yield chunk
                    if len(head) < 2048:  # an upstream error body, kept for the trust check
                        head += chunk[: 2048 - len(head)]
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        _scan_sse_line(line, acc, t0)
            except BaseException as e:
                err = f"stream interrupted: {type(e).__name__}"
                raise
            finally:
                energy = self.meter.end(call_id, up.gpu)
                data = {"usage": acc["usage"] or {}, "choices": [{"finish_reason": acc["finish"], "message": {
                    "tool_calls": [{"function": {"name": n}} for n in acc["tools"]]}}]}
                status = r.status_code if err is None else 599
                if err is None and r.status_code >= 400:
                    err = head.decode(errors="replace")[:1000] or f"upstream HTTP {r.status_code}"
                self._spawn(self._finish_chat_stream(r, call_id, t0, up, ctx, req, data, status, err, energy,
                                                     acc["ttft"]))

        return StreamingResponse(relay(), status_code=r.status_code,
                                 media_type=r.headers.get("content-type", "text/event-stream"))

    # ---- Responses API (Codex) and Anthropic Messages API (Claude Code): metered pass-through --------
    async def passthrough(self, request: Request, api: str) -> Response:
        """Relay /v1/responses or /v1/messages to the chosen tier (SGLang serves both natively), metering
        usage from each format. Only tier-changing savers (downgrade) apply to these formats."""
        body = await request.json()
        role, run_id, task_key, session_id = self.meta_from(request, body)
        tier = self.cfg.tier_for(body.get("model"), role)
        ctx = CallContext(self, body, tier, role, run_id, task_key, session_id)
        if role == "agent":
            for sv in self.savers:
                if sv.name == "downgrade":
                    await sv.before(ctx)
        up = self.cfg.upstreams[ctx.tier]
        req = {**body, "model": up.model}
        if up.max_tokens_cap:  # Claude Code asks for up to 128k output tokens; local servers allow far fewer
            k = "max_output_tokens" if api == "responses" else "max_tokens"
            req[k] = min(req.get(k) or up.max_tokens_cap, up.max_tokens_cap)
        if up.strip_grammar and isinstance(req.get("tools"), list):  # see _prepare: no constrained decoding locally
            req["tools"] = [{k: v for k, v in t.items() if k != "strict"} if isinstance(t, dict) else t for t in req["tools"]]
        path = "responses" if api == "responses" else "messages"
        call_id, t0 = uuid.uuid4().hex, time.time()
        self.meter.begin(call_id, up.gpu)
        acc = {"usage": {}, "tools": [], "ttft": None, "finish": None}
        try:
            upstream_req = self.client.build_request("POST", f"{up.base_url}/{path}", json=req, headers=_api_headers(up))
            r = await self.client.send(upstream_req, stream=True)
        except httpx.HTTPError as e:
            energy = self.meter.end(call_id, up.gpu)
            err = f"{type(e).__name__}: {e}"
            await self._book_api(call_id, t0, up, ctx, api, req, acc, 599, err, energy, bool(body.get("stream")))
            return JSONResponse({"error": {"message": err}}, status_code=502)

        if not body.get("stream"):
            raw, status, err = b"", r.status_code, None
            try:
                raw = await r.aread()
            except httpx.HTTPError as e:  # the body failed mid-read: still close, end metering and book
                status, err = 599, f"{type(e).__name__}: {e}"
            except BaseException as e:  # cancelled mid-read: book it as interrupted, then re-raise
                energy = self.meter.end(call_id, up.gpu)
                self._spawn(self._finish_stream(r, call_id, t0, up, ctx, api, req, acc, 599,
                                                f"interrupted: {type(e).__name__}", energy))
                raise
            try:
                await r.aclose()
            except Exception:
                pass
            energy = self.meter.end(call_id, up.gpu)
            if err is None:
                try:
                    _scan_api_object(json.loads(raw), api, acc)
                except ValueError:
                    pass
                if status >= 400:
                    err = raw[:2000].decode(errors="replace")
            await self._book_api(call_id, t0, up, ctx, api, req, acc, status, err, energy, False)
            if status == 599:
                return JSONResponse({"error": {"message": err}}, status_code=502)
            return Response(raw, status_code=status, media_type="application/json")

        async def relay():
            buf, err, head = b"", None, b""
            try:
                async for chunk in r.aiter_bytes():
                    yield chunk
                    if len(head) < 2048:  # an upstream error body, kept for the trust check
                        head += chunk[: 2048 - len(head)]
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if line.startswith(b"data:") and line[5:].strip() not in (b"", b"[DONE]"):
                            try:
                                ev = json.loads(line[5:].strip())
                            except ValueError:
                                continue
                            if acc["ttft"] is None:
                                acc["ttft"] = (time.time() - t0) * 1000.0
                            _scan_api_event(ev, api, acc)
            except BaseException as e:  # client disconnect, cancellation or upstream read error
                err = f"stream interrupted: {type(e).__name__}"
                raise
            finally:
                energy = self.meter.end(call_id, up.gpu)  # synchronous: always runs
                status = r.status_code if err is None else 599
                if err is None and r.status_code >= 400:
                    err = head.decode(errors="replace")[:1000] or f"upstream HTTP {r.status_code}"
                self._spawn(self._finish_stream(r, call_id, t0, up, ctx, api, req, acc, status, err, energy))

        return StreamingResponse(relay(), status_code=r.status_code,
                                 media_type=r.headers.get("content-type", "text/event-stream"))

    def _spawn(self, coro) -> None:
        """Run a finalizer to completion even if the request that started it is cancelled; drained at shutdown."""
        t = asyncio.ensure_future(coro)
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def _finish_stream(self, r, call_id, t0, up, ctx, api, req, acc, status, err, energy):
        try:
            await r.aclose()
        except Exception:
            pass
        await self._book_api(call_id, t0, up, ctx, api, req, acc, status, err, energy, True)

    async def _book_api(self, call_id, t0, up, ctx, api, req, acc, status, err, energy, stream):
        u = acc["usage"]
        unknown = not u and err is not None  # interrupted before usage arrived: tokens and cost are unknown, not 0
        if api == "responses":
            cached = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
            inp, cw = max(0, (u.get("input_tokens") or 0) - cached), 0
            reasoning = (u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
        else:  # Anthropic: input_tokens excludes cache reads and writes
            cached = u.get("cache_read_input_tokens") or 0
            cw = u.get("cache_creation_input_tokens") or 0
            inp, reasoning = u.get("input_tokens") or 0, 0
        out = u.get("output_tokens") or 0
        n_msgs = len(req.get("input") or req.get("messages") or []) if isinstance(req.get("input") or req.get("messages"), list) else 1
        row = {"call_id": call_id, "ts_start": t0, "ts_end": time.time(), "source": self.cfg.source, "run_id": ctx.run_id,
               "task_key": ctx.task_key, "session_id": ctx.session_id, "role": ctx.role, "tier": ctx.tier,
               "model": up.model, "api": api, "stream": int(stream), "input_tokens": inp, "cache_read_tokens": cached,
               "cache_write_tokens": cw, "output_tokens": out, "reasoning_tokens": reasoning, "n_messages": n_msgs,
               "n_tool_calls": len(acc["tools"]), "tool_names": acc["tools"], "finish_reason": acc["finish"],
               "status": status, "error": err, "latency_ms": (time.time() - t0) * 1000.0, "ttft_ms": acc["ttft"],
               "gpu": up.gpu, "energy_j": energy,
               "cost_usd": None if unknown else pricing.cost_usd(up.price_model, inp, cached, cw, out),
               "saver": ctx.notes or None, "decision": ctx.decision,
               "meta": {"step": ctx.step, "requested_model": req.get("model")} if ctx.step else None}
        await asyncio.to_thread(self.ledger.add_call, row)

    async def _finish_chat_stream(self, r, call_id, t0, up, ctx, req, data, status, err, energy, ttft):
        try:
            await r.aclose()
        except Exception:
            pass
        await self._book(call_id, t0, time.time(), up, ctx.tier, ctx.role, ctx.run_id, ctx.task_key, ctx.session_id,
                         req, data, status, err, energy, stream=True, ctx=ctx, ttft=ttft)

    async def models(self) -> dict:
        return {"object": "list", "data": [{"id": u.model, "object": "model", "owned_by": f"truecost:{n}"}
                                           for n, u in self.cfg.upstreams.items()]}

    async def aclose(self):
        await self.client.aclose()
        self.meter.stop()


def _api_headers(up: Upstream) -> dict:
    """Auth for the Responses and Anthropic Messages pass-through: both header styles, so either kind of server accepts it."""
    return {"Authorization": f"Bearer {up.api_key}", "x-api-key": up.api_key, "anthropic-version": "2023-06-01"}


_CTX_RE = re.compile(r"maximum context length of (\d+).*?(\d+) tokens from the input", re.S)


def _fit_max_tokens(raw: dict, req: dict) -> int | None:
    """max_tokens that fits the context window, if the upstream rejected the call for exceeding it."""
    if raw["status"] != 400:
        return None
    m = _CTX_RE.search(json.dumps(raw["data"]))
    if not m:
        return None
    room = int(m.group(1)) - int(m.group(2)) - 64
    asked = req.get("max_tokens") or req.get("max_completion_tokens") or 0
    return room if 256 <= room < asked else None


def _scan_sse_line(line: bytes, acc: dict, t0: float) -> None:
    line = line.strip()
    if not line.startswith(b"data:"):
        return
    payload = line[5:].strip()
    if payload == b"[DONE]":
        return
    try:
        ev = json.loads(payload)
    except ValueError:
        return
    if ev.get("usage"):
        acc["usage"] = ev["usage"]
    for ch in ev.get("choices") or []:
        d = ch.get("delta") or {}
        if acc["ttft"] is None and (d.get("content") or d.get("tool_calls") or d.get("reasoning_content")):
            acc["ttft"] = (time.time() - t0) * 1000.0
        for tc in d.get("tool_calls") or []:
            name = (tc.get("function") or {}).get("name")
            if name:
                acc["tools"].append(name)
        if ch.get("finish_reason"):
            acc["finish"] = ch["finish_reason"]


def _scan_api_object(obj: dict, api: str, acc: dict) -> None:
    """Usage and tool calls from a complete Responses or Messages object."""
    if api == "responses":
        acc["usage"] = obj.get("usage") or acc["usage"]
        acc["finish"] = obj.get("status") or acc["finish"]
        for item in obj.get("output") or []:
            if item.get("type") in ("function_call", "custom_tool_call"):
                acc["tools"].append(item.get("name"))
    else:
        acc["usage"] = {**acc["usage"], **(obj.get("usage") or {})}
        acc["finish"] = obj.get("stop_reason") or acc["finish"]
        for b in obj.get("content") or []:
            if b.get("type") == "tool_use":
                acc["tools"].append(b.get("name"))


def _scan_api_event(ev: dict, api: str, acc: dict) -> None:
    t = ev.get("type", "")
    if api == "responses":
        if t in ("response.completed", "response.incomplete", "response.failed") and ev.get("response"):
            _scan_api_object(ev["response"], api, acc)
        elif t == "response.output_item.added" and (ev.get("item") or {}).get("type") in ("function_call", "custom_tool_call"):
            pass  # counted from the final object
    else:
        if t == "message_start":
            acc["usage"] = {**acc["usage"], **((ev.get("message") or {}).get("usage") or {})}
        elif t == "message_delta":
            acc["usage"] = {**acc["usage"], **{k: v for k, v in (ev.get("usage") or {}).items() if v is not None}}
            acc["finish"] = (ev.get("delta") or {}).get("stop_reason") or acc["finish"]
        elif t == "content_block_start" and (ev.get("content_block") or {}).get("type") == "tool_use":
            acc["tools"].append(ev["content_block"].get("name"))


async def _as_sse(data: dict):
    msg = data["choices"][0]["message"]
    chunk = {"id": data.get("id") or f"truecost-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
             "created": data.get("created") or int(time.time()), "model": data.get("model", "truecost"),
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": msg.get("content"),
                                                "tool_calls": [{**t, "index": i} for i, t in enumerate(msg.get("tool_calls") or [])] or None,
                                                # a reasoning model's thinking, so a replayed reply is the same reply
                                                **({"reasoning_content": msg["reasoning_content"]}
                                                   if msg.get("reasoning_content") else {})},
                          "finish_reason": data["choices"][0].get("finish_reason")}]}
    yield f"data: {json.dumps(chunk)}\n\n".encode()
    if data.get("usage"):
        yield f"data: {json.dumps({**chunk, 'choices': [], 'usage': data['usage']})}\n\n".encode()
    yield b"data: [DONE]\n\n"


def create_app(cfg: Config, **kwargs) -> FastAPI:
    gw = Gateway(cfg, **kwargs)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        gw.meter.start()
        if cfg.run:
            gw.ledger.upsert_run({"run_id": cfg.run, "workload": cfg.workload, "condition": cfg.condition,
                                  "config": {"savers": [s.describe() for s in gw.savers],
                                             "governor": gw.governor.describe() if gw.governor else None,
                                             "upstreams": {k: {"model": u.model, "gpu": u.gpu}
                                                           for k, u in cfg.upstreams.items()}},
                                  "started": time.time()})
        yield
        if cfg.run:
            snap = gw.meter.snapshot()
            gw.ledger.upsert_run({"run_id": cfg.run, "ended": time.time()})
            for g, tot in snap["total_j"].items():
                idle = snap["idle_j"].get(g, 0.0)
                gw.ledger.set_energy(cfg.run, g, tot, tot - idle, idle)
        await gw.drain()  # finish any booking still in flight
        await gw.aclose()

    app = FastAPI(title="TrueCost gateway", lifespan=lifespan)
    app.state.gw = gw

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await gw.chat(request)

    @app.post("/v1/responses")
    async def responses(request: Request):
        return await gw.passthrough(request, "responses")

    @app.post("/v1/messages")
    async def messages(request: Request):
        return await gw.passthrough(request, "messages")

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request):
        body = await request.json()
        up = gw.cfg.upstreams[gw.cfg.tier_for(body.get("model"), "agent")]
        r = await gw.client.post(f"{up.base_url}/messages/count_tokens", json={**body, "model": up.model},
                                 headers=_api_headers(up))
        return Response(r.content, status_code=r.status_code, media_type="application/json")

    @app.get("/v1/models")
    async def models():
        return await gw.models()

    @app.get("/health")
    async def health():
        return {"ok": True, "run": cfg.run, "energy": gw.meter.snapshot() if gw.meter.available else None}

    @app.get("/truecost/energy")
    async def energy():
        return gw.meter.snapshot()

    return app
