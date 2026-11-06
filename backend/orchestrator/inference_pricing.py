"""
Model-specific, VERIFIED pricing catalogue (USD per 1M tokens).

There is deliberately NO generic fallback price: an unknown model's cost is
reported as UNKNOWN, and a session with a dollar cap cannot use a model that
has no verified price.

Fill backend/pricing.json (or PRICING_FILE) from the Vultr Serverless
Inference model pricing sheet in the Vultr console, for exactly the models
you enable:

{
  "<model-id-as-listed-by-/v1/models>": {
    "input_per_million": 0.00, "output_per_million": 0.00,
    "currency": "USD", "source": "Vultr console model pricing sheet",
    "verified_at": "2026-11-06"
  }
}
PRICING_OVERRIDES (same JSON shape, env var) is merged on top.
"""

import json
import os
from pathlib import Path

CATALOGUE_VERSION_KEY = "_version"


class UnknownModelPricing(ValueError):
    pass


def _load() -> dict[str, dict]:
    out: dict[str, dict] = {}
    path = Path(os.environ.get("PRICING_FILE", Path(__file__).resolve().parents[1] / "pricing.json"))
    sources = []
    if path.exists():
        try:
            sources.append(json.loads(path.read_text()))
        except Exception:
            pass
    if os.environ.get("PRICING_OVERRIDES"):
        try:
            sources.append(json.loads(os.environ["PRICING_OVERRIDES"]))
        except Exception:
            pass
    for src in sources:
        for model, p in src.items():
            if model.startswith("_") or not isinstance(p, dict):
                continue
            try:
                entry = {"input_per_million": float(p["input_per_million"]),
                         "output_per_million": float(p["output_per_million"]),
                         "currency": p.get("currency", "USD"), "source": p.get("source", ""),
                         "verified_at": p.get("verified_at", "")}
            except (KeyError, TypeError, ValueError):
                continue
            if entry["input_per_million"] > 0 and entry["output_per_million"] > 0 and entry["verified_at"]:
                out[model] = entry
    return out


PRICING: dict[str, dict] = _load()


def reload() -> None:
    PRICING.clear()
    PRICING.update(_load())


def has_price(model_id: str) -> bool:
    return model_id in PRICING


def price_for(model_id: str) -> dict:
    p = PRICING.get(model_id)
    if p is None:
        raise UnknownModelPricing(f"no verified pricing configured for model {model_id!r}")
    return p


def cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float | None:
    """Estimated cost, or None when the model has no verified price."""
    p = PRICING.get(model_id)
    if p is None:
        return None
    return input_tokens / 1e6 * p["input_per_million"] + output_tokens / 1e6 * p["output_per_million"]


def is_strictly_cheaper(default_model: str, candidate: str) -> bool:
    """Cheaper on BOTH input and output; otherwise not advertised as cheaper."""
    if not (has_price(default_model) and has_price(candidate)):
        return False
    d, c = PRICING[default_model], PRICING[candidate]
    return c["input_per_million"] < d["input_per_million"] and c["output_per_million"] < d["output_per_million"]
