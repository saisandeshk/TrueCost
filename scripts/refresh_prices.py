"""Snapshot list prices (USD per 1M tokens) into truecost/data/prices.json.

Source: OpenRouter's public model list (no key needed). Models it does not list get a manual entry
with its source named, e.g. Artificial Analysis' median provider price. Run: python scripts/refresh_prices.py
"""
import datetime as dt
import json
import pathlib
import urllib.request

OUT = pathlib.Path(__file__).resolve().parents[1] / "truecost" / "data" / "prices.json"

# Models we price. Keys are OpenRouter ids.
WANT = [
    "qwen/qwen3.5-9b", "qwen/qwen3.8-27b", "qwen/qwen3.6-35b-a3b", "qwen/qwen3-coder-30b-a3b-instruct",
    "nvidia/nemotron-3.5-lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3-nano-30b-a3b",
    "anthropic/claude-fable-5.1", "anthropic/claude-fable-5", "anthropic/claude-opus-5.5",
    "anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-5.5",
    "openai/gpt-6.1-sol", "openai/gpt-6-sol", "openai/gpt-6-astra", "openai/gpt-6-luna",
    "openai/gpt-5.6-sol", "openai/gpt-5.6-terra", "openai/gpt-5.6-luna",
]
AA = "Artificial Analysis median provider price, 2026-10-20 (artificialanalysis.ai/models/{})"
# The Qwen tiers are priced from Artificial Analysis (median across providers), so tiers are priced on one
# consistent basis; single-provider OpenRouter prices for them are uneven (e.g. a 27B input price below a 9B's).
# Everything else, Nemotron included, keeps its OpenRouter price.
# Cache hits: the Qwen API discounts cached input by 80% (shown on the Qwen3.8-27B page). Artificial Analysis lists
# no cache price for the other Qwen tiers, so the same 80% discount is applied to all of them: otherwise a tier
# with a listed discount looks cheaper per cached token than a smaller tier without one (an artefact, not a price).
CACHE_NOTE = "; cache hit = 80% discount (Qwen API), applied to every Qwen tier for consistency"
MANUAL = {
    "qwen/qwen3.5-4b": {"in": 0.03, "out": 0.15, "cache_read": 0.006, "cache_write": None,
                        "source": AA.format("qwen3-5-4b") + CACHE_NOTE},
    "qwen/qwen3.5-9b": {"in": 0.14, "out": 0.20, "cache_read": 0.028, "cache_write": None,
                        "source": AA.format("qwen3-5-9b") + CACHE_NOTE},
    "qwen/qwen3.8-27b": {"in": 0.50, "out": 3.00, "cache_read": 0.10, "cache_write": None,
                         "source": AA.format("qwen3-8-27b") + CACHE_NOTE},
    "qwen/qwen3.6-35b-a3b": {"in": 0.375, "out": 2.25, "cache_read": 0.075, "cache_write": None,
                             "source": AA.format("qwen3-6-35b-a3b") + CACHE_NOTE},
}
# Names that agents, servers and logs use -> priced id.
ALIASES = {
    "qwen3.5-4b": "qwen/qwen3.5-4b", "qwen3.5-9b": "qwen/qwen3.5-9b", "qwen3.8-27b": "qwen/qwen3.8-27b",
    "qwen3.6-35b-a3b": "qwen/qwen3.6-35b-a3b", "qwen3-coder-30b": "qwen/qwen3-coder-30b-a3b-instruct",
    "claude-fable-5-1": "anthropic/claude-fable-5.1", "claude-fable-5": "anthropic/claude-fable-5",
    "claude-opus-5-5": "anthropic/claude-opus-5.5", "claude-sonnet-5-5": "anthropic/claude-sonnet-5.5",
    "claude-haiku-5-5": "anthropic/claude-haiku-5.5",
    "gpt-6.1-sol": "openai/gpt-6.1-sol", "gpt-6-sol": "openai/gpt-6-sol", "gpt-6-astra": "openai/gpt-6-astra",
    "gpt-6-luna": "openai/gpt-6-luna", "gpt-5.6-sol": "openai/gpt-5.6-sol", "gpt-5.6-terra": "openai/gpt-5.6-terra",
    "gpt-5.6-luna": "openai/gpt-5.6-luna",
}


def per_m(x):
    return None if x in (None, "", "-1") else round(float(x) * 1e6, 6)


def main():
    with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=60) as r:
        data = {m["id"]: m for m in json.load(r)["data"]}
    today = dt.date.today().isoformat()
    models = {}
    for mid in WANT:
        m = data.get(mid)
        if m is None:
            print("missing on OpenRouter:", mid)
            continue
        p = m.get("pricing", {})
        models[mid] = {"in": per_m(p.get("prompt")), "out": per_m(p.get("completion")),
                       "cache_read": per_m(p.get("input_cache_read")),
                       "cache_write": per_m(p.get("input_cache_write")),
                       "source": f"OpenRouter public model list, {today}"}
    models.update(MANUAL)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"snapshot": today, "unit": "USD per 1M tokens", "models": models,
                               "aliases": ALIASES}, indent=1) + "\n")
    print(f"wrote {len(models)} models to {OUT}")


if __name__ == "__main__":
    main()
