"""The TrueCost governor: the cheap model drafts every step; a calibrated gate keeps the draft or the
strong model redoes the step; sensitive (write) actions are pinned to the strong model.

Modes (spec["mode"]):
  record   the strong model answers every step; the cheap model drafts in the shadow (role "shadow",
           not counted as agent cost) and each (draft, strong) pair is written to a twin file. These
           pairs are the calibration data: no separate replay runs are needed.
  cascade  draft -> pin check -> gate score >= threshold ? keep the draft : strong redoes the step.
           The threshold comes from conformal risk control at the declared alpha (truecost.calibrate).
  naive    the same cascade with an uncalibrated confidence (mean token probability) at a fixed vendor
           threshold and no pins.
  router   a request-reading decision model (von 1.3 via evaluation/serving/von_service.py) picks the tier for each
           step from the customer's latest message, argmax and no pins: how Jev/RouteLLM-style routers work,
           and the "router" saver in the crash-test (the target of the P.S. injection attack).
Every decision is booked on the call row (ledger `decision`), so receipts show why each step ran where.

Policy check (spec["policy"] = a domain of truecost.safety, e.g. "retail"): before a
write reaches the agent, the conversation is checked against the written policy with the safety checker's own rules.
A blocked write is never returned: the strong model is asked again with a note naming the missing step (booked as
"policy-reask"), and if it still breaks a rule a fixed reply takes its place (ask for identity / confirmation / the
cancel reason, or read the order first; booked as "policy-fallback").
"""

from __future__ import annotations

import json
import math
import re
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .gateway import CallContext

WRITE_VERBS = re.compile(
    r"(^|_)(cancel|modify|update|return|exchange|book|send|create|delete|remove|post|add|approve|pay|refund|"
    r"transfer|issue|set|write|insert|upsert|schedule|invite|assign|close|reopen|submit|charge|grant|revoke|"
    r"archive|move|reply|forward|merge|edit|change)(_|$)")
READ_PREFIX = re.compile(r"^(get|find|list|search|read|lookup|fetch|calculate|think|check|view|query)(_|$)")
LN_HALF = math.log(0.5)


def is_write(tool: str | None, args: dict | None = None) -> bool:
    """Heuristic: does this tool call change state? Explicit pins in the spec take precedence."""
    if not tool:
        return False
    t = tool.lower()
    if t in ("api_fetch", "http_request", "fetch", "request") and isinstance(args, dict):
        return str(args.get("method", "GET")).upper() in ("POST", "PUT", "PATCH", "DELETE")
    if READ_PREFIX.search(t):
        return False
    return bool(WRITE_VERBS.search(t))


def _calls(msg: dict) -> list[tuple[str | None, dict]]:
    """Every tool call in a message (agents may issue several per turn)."""
    out = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {"_raw": fn.get("arguments")}
        out.append((fn.get("name"), args if isinstance(args, dict) else {"_value": args}))
    return out


def _first_call(msg: dict) -> tuple[str | None, dict]:
    tcs = msg.get("tool_calls") or []
    if not tcs:
        return None, {}
    fn = tcs[0].get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        args = {"_raw": fn.get("arguments")}
    return fn.get("name"), args if isinstance(args, dict) else {"_value": args}


def features(data: dict, body: dict, step: int | None) -> dict[str, float]:
    """Gate features from a cheap draft: token confidence (overall, the act/talk decision, the tool name,
    the arguments) plus a little context."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    toks = [(t.get("token", ""), t.get("logprob")) for t in ((choice.get("logprobs") or {}).get("content") or [])
            if t.get("logprob") is not None]
    lps = [lp for _, lp in toks]
    name_lps, arg_lps, zone = [], [], None
    for tok, lp in toks:
        if zone is None and "function" in tok:
            zone = "name"
        if zone == "name":
            name_lps.append(lp)
            if ">" in tok:
                zone = "args"
                continue
        elif zone == "args":
            arg_lps.append(lp)
    tool, args = _first_call(msg)
    calls = _calls(msg)
    msgs = body.get("messages") or []
    last_tool = next((m for m in reversed(msgs) if m.get("role") == "tool"), None)
    last_err = 1.0 if last_tool and str(last_tool.get("content", "")).lower().startswith("error") else 0.0
    return {
        "lp_mean": sum(lps) / len(lps) if lps else 0.0,
        "lp_min": min(lps) if lps else 0.0,
        "lp_first": lps[0] if lps else 0.0,
        "lp_name_min": min(name_lps) if name_lps else 0.0,
        "lp_args_min": min(arg_lps) if arg_lps else 0.0,
        "low_frac": sum(1 for x in lps if x < LN_HALF) / len(lps) if lps else 0.0,
        "n_tok_log": math.log1p(len(lps)),
        "is_tool": 1.0 if tool else 0.0,
        "is_write": 1.0 if any(is_write(n, a) for n, a in calls) else 0.0,
        "step_log": math.log1p(step or 0),
        "n_msgs_log": math.log1p(len(msgs)),
        "last_tool_error": last_err,
    }


# ---- comparing a draft with the strong model's action ------------------------------------------
_ENT = re.compile(r"#?[A-Z]\d{5,}|\b\d+(?:\.\d+)?\b|[\w.+-]+@[\w-]+\.[\w.]+|\b[a-z]+_[a-z]+_\d+\b")


def _norm(v):
    """Canonical form for comparing arguments: case and list order are kept (ids, paths, argv and ordered
    operations can depend on them). Erring towards "different" only makes the gate more conservative."""
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in sorted(v.items())}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def compare(draft: dict, strong: dict, context: str = "") -> tuple[bool, str]:
    """Does the cheap draft take the same action as the strong model? Tool calls must match exactly
    (name and normalised arguments). Text replies match when both ask (or both do not ask) a question and
    the draft states no number, id or email that is absent from the strong reply and the conversation."""
    dc, sc = _calls(draft), _calls(strong)
    if dc or sc:
        if not dc or not sc:
            return False, "act_vs_talk"
        if [n or "" for n, _ in dc] != [n or "" for n, _ in sc]:
            return False, "tool_name"
        key = lambda c: json.dumps([c[0], _norm(c[1])], sort_keys=True)
        return list(map(key, dc)) == list(map(key, sc)), "tool_args"  # same actions, same order
    dtext, stext = draft.get("content") or "", strong.get("content") or ""
    if ("?" in dtext) != ("?" in stext):
        return False, "text_intent"
    known = set(_ENT.findall(stext)) | set(_ENT.findall(context))
    extra = set(_ENT.findall(dtext)) - known
    return (not extra), "text"


# ---- the pre-write policy check --------------------------------------------------------------
POLICY_BLOCK = ("auth_first", "other_user", "status_first", "cancel_reason", "confirm_first")  # fallback priority
POLICY_HINT = {
    "auth_first": "the customer's identity has not been verified yet (look up their user id first)",
    "other_user": "the order or account belongs to someone other than the authenticated customer",
    "status_first": "you have not read this order's details yet",
    "cancel_reason": "a cancellation reason must be 'no longer needed' or 'ordered by mistake'",
    "confirm_first": "the customer has not explicitly confirmed this action since you last spoke: list the action "
                     "and its details and ask them to confirm with a yes",
}


def _text(c: Any) -> str:
    if isinstance(c, list):  # content parts
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


def _transcript(messages: list[dict]) -> list[dict]:
    """The agent's OpenAI chat messages in the transcript shape truecost.safety.check reads."""
    out = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": _text(m.get("content"))})
        elif role == "assistant":
            out.append({"role": "assistant", "content": _text(m.get("content")),
                        "tool_calls": [{"id": tc.get("id"), "name": (tc.get("function") or {}).get("name"),
                                        "arguments": (tc.get("function") or {}).get("arguments")}
                                       for tc in m.get("tool_calls") or []]})
        elif role == "tool":
            c = _text(m.get("content"))
            out.append({"role": "tool", "id": m.get("tool_call_id"), "content": c,
                        "error": c.strip().lower().startswith("error")})
    return out


def policy_violations(messages: list[dict], msg: dict, domain: str) -> list[dict]:
    """The blocking rules a candidate reply would break, judged by the safety checker on the conversation so far."""
    from .safety import check

    conv = _transcript([*messages, {**msg, "role": "assistant"}])
    return [v for v in check(conv, domain) if v["step"] == len(conv) - 1 and v["rule"] in POLICY_BLOCK]


def policy_note(bad: list[dict]) -> str:
    rules = sorted({v["rule"] for v in bad}, key=POLICY_BLOCK.index)
    tool = bad[0]["tool"]
    return (f"POLICY CHECK (TrueCost governor): your proposed action `{tool}` was blocked because "
            + "; ".join(POLICY_HINT[r] for r in rules) + f". Do the missing step first; do not call `{tool}` until then.")


def policy_fallback(bad: list[dict], msg: dict, domain: str) -> dict:
    """A fixed reply that does the first missing step instead of the blocked write."""
    from .safety import DOMAINS

    rule = min((v["rule"] for v in bad), key=POLICY_BLOCK.index)
    v = next(x for x in bad if x["rule"] == rule)
    if rule == "status_first" and DOMAINS[domain]["order_reads"]:
        read = sorted(DOMAINS[domain]["order_reads"])[0]
        key = "reservation_id" if "reservation" in read else "order_id"
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call_tc{uuid.uuid4().hex[:12]}", "type": "function",
            "function": {"name": read, "arguments": json.dumps({key: v["detail"]})}}]}
    if rule == "auth_first":
        text = ("Before I can make any changes, I need to verify your identity. Could you please give me your email "
                "address, or your full name and zip code?")
    elif rule == "other_user":
        text = "I'm sorry, but I can only make changes to orders on your own account."
    elif rule == "cancel_reason":
        text = "Before I cancel, could you tell me the reason: is the order no longer needed, or was it ordered by mistake?"
    else:
        args = next((a for n, a in _calls(msg) if n == v["tool"]), {})
        details = ", ".join(f"{k.replace('_', ' ')}: {json.dumps(x) if not isinstance(x, str) else x}" for k, x in args.items())
        text = f"Before I proceed, please confirm: I will {v['tool'].replace('_', ' ')} ({details}). Shall I go ahead (yes/no)?"
    return {"role": "assistant", "content": text}


class GateModel:
    """Logistic gate over draft features, P(draft matches the strong model), with conformal thresholds."""

    def __init__(self, d: dict):
        self.names = d["features"]
        self.coef, self.intercept = d["coef"], d["intercept"]
        self.mean, self.std = d["mean"], d["std"]
        self.thresholds = {float(k): v for k, v in (d.get("thresholds") or {}).items()}

    @classmethod
    def load(cls, path: str | Path) -> "GateModel":
        return cls(json.loads(Path(path).expanduser().read_text()))

    def score(self, f: dict[str, float]) -> float:
        z = self.intercept + sum(c * ((f.get(n, 0.0) - m) / (s or 1.0))
                                 for n, c, m, s in zip(self.names, self.coef, self.mean, self.std))
        return 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, z))))

    def threshold_for(self, alpha: float) -> float:
        if alpha in self.thresholds:
            return self.thresholds[alpha]
        lower = [a for a in self.thresholds if a <= alpha]
        return self.thresholds[max(lower)] if lower else 1.01  # no calibrated level: never accept


class Governor:
    def __init__(self, spec: dict, run_id: str | None = None, out_dir: str | None = None):
        self.spec = spec
        self.mode = spec.get("mode", "cascade")
        self.cheap, self.strong = spec.get("cheap", "cheap"), spec.get("strong", "strong")
        self.alpha = spec.get("alpha")
        pins = spec.get("pins", "auto")
        self.pins_auto = pins == "auto"
        self.pins = set(pins) if isinstance(pins, list) else set()
        self.gate = GateModel.load(spec["gate"]) if spec.get("gate") else None
        self.policy = spec.get("policy")  # a truecost.safety domain whose rules every write must pass
        self.policy_retries = int(spec.get("policy_retries", 1))
        if self.mode in ("naive", "router"):
            self.threshold = float(spec.get("threshold", 0.8 if self.mode == "naive" else 0.0))
            self.pins_auto, self.pins = False, set()
        elif spec.get("threshold") is not None:
            self.threshold = float(spec["threshold"])
        elif self.gate and self.alpha is not None:
            self.threshold = self.gate.threshold_for(float(self.alpha))
        else:
            self.threshold = 1.01
        self._lock = threading.Lock()
        self.twin_path = None
        if self.mode == "record":
            base = Path(out_dir or spec.get("twins_dir") or "~/.truecost/twins").expanduser()
            base.mkdir(parents=True, exist_ok=True)
            self.twin_path = base / f"{run_id or 'run'}.jsonl"

    def describe(self) -> dict:
        return {"mode": self.mode, "alpha": self.alpha, "threshold": self.threshold,
                "pins": "auto" if self.pins_auto else sorted(self.pins), "policy": self.policy}

    def pinned(self, msg: dict) -> str | None:
        """The first proposed tool call that must run on the strong tier, checking EVERY call in the draft."""
        for tool, args in _calls(msg):
            if tool and (tool in self.pins or (self.pins_auto and is_write(tool, args))):
                return tool
        return None

    def score(self, f: dict[str, float]) -> float:
        if self.mode == "naive" or self.gate is None:
            return math.exp(f["lp_mean"])  # mean token probability, uncalibrated
        return self.gate.score(f)

    async def ask_decision_model(self, ctx: "CallContext", url: str) -> dict | None:
        msgs = ctx.body.get("messages") or []
        user = next((m.get("content") for m in reversed(msgs) if m.get("role") == "user"), "") or ""
        last_tool = next((m.get("name") or "" for m in reversed(msgs) if m.get("role") == "tool"), "")
        try:
            r = await ctx.gw.client.post(url, json={"customer_message": str(user)[-2000:], "last_tool": last_tool}, timeout=30)
            return r.json()
        except Exception:
            return None

    async def route(self, ctx: "CallContext") -> None:
        d = await self.ask_decision_model(ctx, self.spec.get("url", "http://127.0.0.1:30030/decide"))
        if d is None:  # router down: fail safe to the strong tier
            ctx.tier, ctx.decision = self.strong, {"action": "fallback", "reason": "router unreachable"}
            return
        small = d.get("choice") == "small" and float(d.get("confidence", 0)) >= float(self.spec.get("threshold", 0.0))
        ctx.tier = self.cheap if small else self.strong
        ctx.decision = {"action": "route-small" if small else "route-large", "score": round(float(d.get("confidence", 0)), 4),
                        "router": "von-1.3"}

    async def before(self, ctx: "CallContext") -> None:
        if ctx.body.get("stream") or self.mode == "off":
            return
        if self.mode == "router":
            await self.route(ctx)
            return
        body = {**ctx.body, "logprobs": True, "top_logprobs": 1}
        raw = await ctx.gw.call_raw(body, self.cheap)
        data = raw["data"]
        if "error" in data:  # the cheap tier failed: fall back to strong
            ctx.tier, ctx.decision = self.strong, {"action": "fallback", "reason": "draft error"}
            await ctx.gw.book_raw(raw, self.cheap, "shadow", ctx, {"action": "draft-error"})
            return
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        f = features(data, ctx.body, ctx.step)
        if self.spec.get("von_url"):  # the System-One decision model's view of the step, as one more gate feature
            v = await self.ask_decision_model(ctx, self.spec["von_url"])
            if v:
                conf = float(v.get("confidence", 0.5))
                f["von_large"] = conf if v.get("choice") == "large" else 1.0 - conf
        tool, args = _first_call(msg)
        if self.mode == "record":
            ctx.tier = self.strong
            ctx.governor_twin = {"draft": msg, "features": f}
            await ctx.gw.book_raw(raw, self.cheap, "shadow", ctx, {"action": "shadow"})
            return
        s = self.score(f)
        base = {"score": round(s, 4), "threshold": self.threshold, "alpha": self.alpha, "draft": tool or "text"}
        pin = self.pinned(msg)
        if pin:
            ctx.tier, ctx.decision = self.strong, {**base, "action": "pin", "pin": pin}
            await ctx.gw.book_raw(raw, self.cheap, "agent", ctx, {**base, "action": "draft-pinned"})
        elif s >= self.threshold:
            ctx.tier = self.cheap  # the step really ran on the cheap tier (budgets price it there)
            ctx.decision = {**base, "action": "accept"}
            await ctx.gw.book_raw(raw, self.cheap, "agent", ctx, ctx.decision)
            for ch in data.get("choices") or []:
                ch.pop("logprobs", None)
            ctx.reply, ctx.reply_booked = data, True
        else:
            ctx.tier, ctx.decision = self.strong, {**base, "action": "escalate"}
            await ctx.gw.book_raw(raw, self.cheap, "agent", ctx, {**base, "action": "draft-rejected"})

    async def police(self, ctx: "CallContext", data: dict) -> None:
        """Policy check: a reply whose write breaks the policy never reaches the agent (it is replaced in place)."""
        choices = data.get("choices") or []
        msg = (choices[0].get("message") or {}) if choices else {}
        if not _calls(msg):
            return
        msgs = ctx.body.get("messages") or []
        bad = policy_violations(msgs, msg, self.policy)
        for attempt in range(1, self.policy_retries + 1):
            if not bad:
                break
            blocked = {"blocked": bad[0]["tool"], "rules": sorted({v["rule"] for v in bad}), "attempt": attempt}
            # the note goes at the end, so the conversation's cached prefix is reused
            body = {**ctx.body, "messages": [*msgs, {"role": "system", "content": policy_note(bad)}]}
            raw = await ctx.gw.call_raw(body, self.strong, ctx)
            await ctx.gw.book_raw(raw, self.strong, "agent", ctx, {"action": "policy-reask", **blocked})
            new = raw["data"]
            if "error" in new or not new.get("choices"):
                break
            cand = new["choices"][0].get("message") or {}
            again = policy_violations(msgs, cand, self.policy)
            if not again:
                data["choices"] = new["choices"]
                return
            bad, msg = again, cand
        if bad:
            fb = policy_fallback(bad, msg, self.policy)
            data["choices"] = [{"index": 0, "message": fb, "finish_reason": "tool_calls" if fb.get("tool_calls") else "stop"}]
            await ctx.gw.book_local(ctx, data, {"action": "policy-fallback", "blocked": bad[0]["tool"],
                                                "rules": sorted({v["rule"] for v in bad})})

    async def after(self, ctx: "CallContext", data: dict) -> None:
        if self.policy and self.mode == "cascade":
            await self.police(ctx, data)
        twin = getattr(ctx, "governor_twin", None)
        if not twin or self.twin_path is None:
            return
        strong = (data.get("choices") or [{}])[0].get("message") or {}
        context = " ".join(str(m.get("content") or "") for m in (ctx.body.get("messages") or [])[-12:])
        match, kind = compare(twin["draft"], strong, context)
        dt, da = _first_call(twin["draft"])
        st, sa = _first_call(strong)
        rec = {"task_key": ctx.task_key, "step": ctx.step, "match": match, "kind": kind, "features": twin["features"],
               "draft_tool": dt, "strong_tool": st,
               "strong_write": any(is_write(n, a) for n, a in _calls(strong)),
               "draft_write": any(is_write(n, a) for n, a in _calls(twin["draft"])),
               "draft_text": (twin["draft"].get("content") or "")[:400], "strong_text": (strong.get("content") or "")[:400],
               "draft_args": da, "strong_args": sa}
        with self._lock, open(self.twin_path, "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
