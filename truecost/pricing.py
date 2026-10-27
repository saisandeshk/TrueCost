"""API-equivalent prices: what a call would cost at public list prices (USD per 1M tokens).

Open models are priced from Artificial Analysis' median provider price; closed models from
OpenRouter's public list. The snapshot lives in data/prices.json (scripts/refresh_prices.py).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data" / "prices.json"


@lru_cache(maxsize=1)
def _table() -> dict:
    return json.loads(DATA.read_text())


def snapshot_date() -> str:
    return _table()["snapshot"]


def resolve(model: str | None) -> str | None:
    """Map a served/logged model name to a priced id, or None if unknown."""
    if not model:
        return None
    t = _table()
    m = model.strip()
    if m in t["models"]:
        return m
    low = m.lower()
    if low in t["aliases"]:
        return t["aliases"][low]
    # Strip provider prefixes and date/variant suffixes: "anthropic/claude-opus-5-5-20260901" etc.
    base = low.split("/")[-1]
    for alias, target in sorted(t["aliases"].items(), key=lambda kv: -len(kv[0])):
        if base.startswith(alias):
            return target
    return None


def price(model: str | None) -> dict | None:
    pid = resolve(model)
    return _table()["models"].get(pid) if pid else None


def cost_usd(model: str | None, input_tokens: int = 0, cache_read_tokens: int = 0,
             cache_write_tokens: int = 0, output_tokens: int = 0) -> float | None:
    """Cost of one call. input_tokens are uncached input; output includes reasoning tokens."""
    p = price(model)
    if p is None:
        return None
    pin = p["in"] or 0.0
    pcr = p["cache_read"] if p.get("cache_read") is not None else pin
    pcw = p["cache_write"] if p.get("cache_write") is not None else pin
    pout = p["out"] or 0.0
    return ((input_tokens or 0) * pin + (cache_read_tokens or 0) * pcr
            + (cache_write_tokens or 0) * pcw + (output_tokens or 0) * pout) / 1e6
