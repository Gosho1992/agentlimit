import asyncio
import json

import httpx
import pytest

from backend.config import settings
from backend.orchestrator import inference_pricing as pricing, model_registry
from backend.orchestrator.inference_adapter import BudgetExceeded, InferenceAdapter
from backend.tests.fakes import mock_inference_transport

MSGS = [{"role": "user", "content": "x" * 300}]


@pytest.fixture
def priced(monkeypatch):
    cat = {"big-model": {"input_per_million": 1.0, "output_per_million": 4.0, "verified_at": "2026-11-06"},
           "small-model": {"input_per_million": 0.1, "output_per_million": 0.3, "verified_at": "2026-11-06"},
           "odd-model": {"input_per_million": 0.1, "output_per_million": 9.0, "verified_at": "2026-11-06"}}
    monkeypatch.setenv("PRICING_OVERRIDES", json.dumps(cat))
    monkeypatch.setenv("PRICING_FILE", "/nonexistent")
    pricing.reload()
    monkeypatch.setattr(settings, "vultr_model_default", "big-model")
    yield
    monkeypatch.delenv("PRICING_OVERRIDES")
    pricing.reload()
    model_registry.STATE.clear()
    model_registry.STATE.update({"validated": False, "downgrade_enabled": False})


def test_no_generic_fallback_price():
    pricing.reload()
    assert pricing.cost_usd("never-heard-of-it", 1000, 1000) is None
    with pytest.raises(pricing.UnknownModelPricing):
        pricing.price_for("never-heard-of-it")


def test_unverified_entries_are_ignored(monkeypatch):
    monkeypatch.setenv("PRICING_OVERRIDES", json.dumps({"m": {"input_per_million": 1, "output_per_million": 1}}))
    pricing.reload()
    assert not pricing.has_price("m")          # no verified_at -> not trusted
    monkeypatch.delenv("PRICING_OVERRIDES")
    pricing.reload()


@pytest.mark.asyncio
async def test_unknown_price_refused_when_cost_cap_enabled(priced):
    inf = InferenceAdapter(token_limit=50_000, cost_limit_usd=1.0, transport=mock_inference_transport())
    with pytest.raises(BudgetExceeded, match="no verified pricing"):
        await inf.chat("unpriced-model", MSGS)


@pytest.mark.asyncio
async def test_unknown_price_without_cap_is_reported_unknown_not_zero(priced):
    inf = InferenceAdapter(token_limit=50_000, transport=mock_inference_transport())
    await inf.chat("unpriced-model", MSGS)
    s = inf.snapshot()
    assert s["unpriced_calls"] == 1 and inf.total_cost_usd == 0.0
    inf2 = InferenceAdapter(token_limit=50_000, transport=mock_inference_transport())
    settings.vultr_model_default = "unpriced-model"
    assert inf2.snapshot()["cost_known"] is False


@pytest.mark.asyncio
async def test_inflight_reservation_counts_in_committed_cost_and_concurrency_is_bounded(priced):
    calls = []
    inf = InferenceAdapter(token_limit=1_000_000, cost_limit_usd=0.0065,
                           transport=mock_inference_transport(calls=calls, delay=0.3))
    first = asyncio.create_task(inf.chat("big-model", MSGS, max_tokens=1500))
    await asyncio.sleep(0.05)
    s = inf.snapshot()
    assert s["in_flight_reserved_cost_usd"] > 0 and s["committed_cost_usd"] == s["in_flight_reserved_cost_usd"]
    # a second concurrent call would push the projected cost over the cap -> refused before any I/O
    with pytest.raises(BudgetExceeded, match="projected"):
        await inf.chat("big-model", MSGS, max_tokens=1500)
    await first
    assert len(calls) == 1
    s = inf.snapshot()
    # settled usage replaced the reservation
    assert s["in_flight_reserved_cost_usd"] == 0 and s["estimated_cost_usd"] == pytest.approx(400 / 1e6 * 1 + 200 / 1e6 * 4)


@pytest.mark.asyncio
async def test_uncertain_reservation_not_discarded_on_termination(priced):
    async def hang(request):
        raise httpx.ReadTimeout("no answer", request=request)
    inf = InferenceAdapter(token_limit=50_000, cost_limit_usd=1.0, transport=httpx.MockTransport(hang))
    with pytest.raises(httpx.ReadTimeout):
        await inf.chat("big-model", MSGS)
    inf.revoke("kill")
    await inf.drain(1)
    s = inf.snapshot()
    assert s["uncertain_requests"] == 1 and s["uncertain_reserved_cost_usd"] > 0
    assert s["committed_cost_usd"] == s["uncertain_reserved_cost_usd"]


def test_downgrade_requires_strictly_cheaper_verified_available_model(priced, monkeypatch):
    monkeypatch.setattr(settings, "vultr_model_cheap", "small-model")
    assert model_registry.validate(["big-model", "small-model"])["downgrade_enabled"]
    monkeypatch.setattr(settings, "vultr_model_cheap", "odd-model")       # cheaper input, pricier output
    assert not model_registry.validate(["big-model", "odd-model"])["downgrade_enabled"]
    monkeypatch.setattr(settings, "vultr_model_cheap", "small-model")
    st = model_registry.validate(["big-model"])                             # not offered by Vultr
    assert not st["downgrade_enabled"] and "not offered" in st["downgrade_reason"]
    monkeypatch.setattr(settings, "vultr_model_cheap", "unpriced-model")
    assert not model_registry.validate(["big-model", "unpriced-model"])["downgrade_enabled"]
    monkeypatch.setattr(settings, "vultr_model_cheap", "")
    assert not model_registry.validate(["big-model"])["downgrade_enabled"]


@pytest.mark.asyncio
async def test_throttled_requests_really_use_the_cheaper_model(priced, monkeypatch):
    monkeypatch.setattr(settings, "vultr_model_cheap", "small-model")
    model_registry.validate(["big-model", "small-model"])
    calls, records = [], []
    inf = InferenceAdapter(token_limit=50_000, transport=mock_inference_transport(calls=calls))
    inf.on_call = records.append
    await inf.chat("big-model", MSGS)
    inf.set_throttle(True)
    await inf.chat("big-model", MSGS)
    assert [c["model"] for c in calls] == ["big-model", "small-model"]
    assert records[1]["requested_model"] == "big-model" and records[1]["effective_model"] == "small-model"
    assert records[1]["reason"] == "governance_state_throttle"
    model_registry.validate(["big-model"])      # downgrade no longer valid
    await inf.chat("big-model", MSGS)
    assert calls[-1]["model"] == "big-model"


@pytest.mark.asyncio
async def test_revoked_agent_never_receives_late_result(priced):
    from backend.orchestrator.inference_adapter import InferenceRevoked
    inf = InferenceAdapter(token_limit=50_000, transport=mock_inference_transport(delay=0.2))
    t = asyncio.create_task(inf.chat("big-model", MSGS))
    await asyncio.sleep(0.05)
    inf.revoke("kill")
    with pytest.raises(InferenceRevoked):
        await t
    assert inf.total_tokens() == 600       # usage still recorded
