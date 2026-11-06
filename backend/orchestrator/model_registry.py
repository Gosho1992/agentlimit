"""
Startup validation of the configured models (fail-safe defaults).

Model downgrade on THROTTLE is enabled ONLY when:
  - the cheap model is configured and differs from the default,
  - both IDs are offered by Vultr's live GET /v1/models,
  - both have verified prices, and
  - the cheap model is strictly cheaper on input AND output.
Until validation has run and passed, downgrade is disabled.
"""

from backend.config import settings
from backend.orchestrator import inference_pricing as pricing

STATE: dict = {"validated": False, "downgrade_enabled": False,
               "downgrade_reason": "model validation has not run"}


def validate(available_models: list[str]) -> dict:
    default, cheap = settings.vultr_model_default, settings.vultr_model_cheap
    st = {
        "validated": True,
        "available_models": available_models,
        "default_model": default,
        "default_available": default in available_models,
        "default_priced": pricing.has_price(default),
        "cheap_model": cheap or None,
        "cheap_available": bool(cheap) and cheap in available_models,
        "cheap_priced": bool(cheap) and pricing.has_price(cheap),
    }
    if not cheap:
        reason, ok = "no VULTR_MODEL_CHEAP configured", False
    elif cheap == default:
        reason, ok = "cheap model equals default model", False
    elif not st["cheap_available"]:
        reason, ok = f"{cheap} is not offered by Vultr /v1/models", False
    elif not (st["default_priced"] and st["cheap_priced"]):
        reason, ok = "verified pricing missing for default and/or cheap model", False
    elif not pricing.is_strictly_cheaper(default, cheap):
        reason, ok = f"{cheap} is not cheaper than {default} on both input and output", False
    else:
        reason, ok = f"{cheap} verified cheaper than {default}", True
    st.update(downgrade_enabled=ok, downgrade_reason=reason)
    STATE.clear()
    STATE.update(st)
    return st


def downgrade_model() -> str | None:
    return settings.vultr_model_cheap if STATE.get("downgrade_enabled") else None
