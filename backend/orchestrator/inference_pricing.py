"""
Per-model token pricing (USD per 1M tokens) used for ESTIMATED cost.

AgentLimit labels every dollar figure as an estimate: authoritative spend is
in the Vultr console Usage tab. Vultr's Serverless Inference docs state
$0.55 per 1M input and $2.75 per 1M output tokens, used here as the default
for any model not listed. Override per model with the PRICING_OVERRIDES env
var, e.g.  PRICING_OVERRIDES='{"my-model": [0.1, 0.25]}'
"""

import json
import os

DEFAULT_PRICE = (0.55, 2.75)

PRICING: dict[str, tuple[float, float]] = {}

_overrides = os.environ.get("PRICING_OVERRIDES", "")
if _overrides:
    try:
        PRICING.update({k: (float(v[0]), float(v[1])) for k, v in json.loads(_overrides).items()})
    except Exception:
        pass


def price_for(model_id: str) -> tuple[float, float]:
    return PRICING.get(model_id, DEFAULT_PRICE)


def cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    p_in, p_out = price_for(model_id)
    return input_tokens / 1_000_000 * p_in + output_tokens / 1_000_000 * p_out
