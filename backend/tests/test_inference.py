import pytest

from backend.orchestrator.context_manager import ContextManager
from backend.orchestrator.inference_adapter import (BudgetExceeded, InferenceAdapter, InferenceRevoked,
                                                     estimate_prompt_tokens)
from backend.tests.fakes import mock_inference_transport

MSGS = [{"role": "system", "content": "s" * 300}, {"role": "user", "content": "u" * 300}]


@pytest.mark.asyncio
async def test_reservation_clamps_max_tokens_to_remaining_budget():
    calls = []
    inf = InferenceAdapter(token_limit=2000, transport=mock_inference_transport(calls=calls))
    inf.total_input_tokens = 1000
    await inf.chat("m", MSGS, max_tokens=1500)
    est = estimate_prompt_tokens(MSGS)
    assert calls[0]["max_tokens"] == 2000 - 1000 - est   # never reserves past the budget


@pytest.mark.asyncio
async def test_reservation_refuses_when_too_little_left():
    inf = InferenceAdapter(token_limit=1300, transport=mock_inference_transport())
    inf.total_input_tokens = 1000
    with pytest.raises(BudgetExceeded):
        await inf.chat("m", MSGS, max_tokens=1500)
    assert inf.refused_calls == 1 and inf.calls == 0


@pytest.mark.asyncio
async def test_cost_cap_reservation():
    inf = InferenceAdapter(token_limit=10_000_000, cost_limit_usd=0.0001, transport=mock_inference_transport())
    with pytest.raises(BudgetExceeded):
        await inf.chat("m", MSGS, max_tokens=1500)


@pytest.mark.asyncio
async def test_revoked_adapter_makes_no_request():
    calls = []
    inf = InferenceAdapter(token_limit=50_000, transport=mock_inference_transport(calls=calls))
    inf.revoke("kill")
    with pytest.raises(InferenceRevoked):
        await inf.chat("m", MSGS)
    assert calls == []


def test_compaction_changes_the_real_prompt():
    cm = ContextManager(max_chars=100_000)
    cm.add("system", "S" * 100)
    cm.add("user", "Task: T")
    for i in range(10):
        cm.add("assistant", "code" * 500)
        cm.add("user", "error" * 500)
    before = ContextManager.size(cm.build())
    cm.max_chars = 6000
    after_msgs = cm.build()
    assert ContextManager.size(after_msgs) <= 6000 + 200 < before
    assert after_msgs[0]["content"] == "S" * 100 and after_msgs[1]["content"] == "Task: T"


@pytest.mark.asyncio
async def test_inflight_request_survives_agent_cancel_and_usage_is_recorded():
    import asyncio
    calls = []
    inf = InferenceAdapter(token_limit=50_000, transport=mock_inference_transport(calls=calls, delay=0.3))
    agent = asyncio.create_task(inf.chat("m", MSGS))
    await asyncio.sleep(0.05)
    assert inf.inflight_summary()["in_flight"] == 1
    assert inf.reserved_tokens() > 0          # counted against the budget while in flight
    inf.revoke("kill")
    agent.cancel()
    summary = await inf.drain(timeout=2)
    assert summary["in_flight"] == 0
    assert inf.total_tokens() == 600          # provider usage recorded although the agent was cancelled
    assert summary["settled_after_revoke"][0]["result_discarded"] is True
    with pytest.raises(InferenceRevoked):
        await inf.chat("m", MSGS)             # no new request after revocation
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unanswered_request_stays_uncertain_not_claimed():
    import asyncio
    import httpx

    async def hang(request):
        raise httpx.ReadTimeout("no response", request=request)
    inf = InferenceAdapter(token_limit=50_000, transport=httpx.MockTransport(hang))
    with pytest.raises(httpx.ReadTimeout):
        await inf.chat("m", MSGS)
    s = inf.inflight_summary()
    assert inf.total_tokens() == 0                    # no usage claimed without a provider response
    assert s["uncertain_requests"] == 1 and s["uncertain_reserved_tokens"] > 0
    assert inf.reserved_tokens() == s["uncertain_reserved_tokens"]   # still counts against budget


@pytest.mark.asyncio
async def test_provider_rejection_releases_reservation():
    import httpx

    async def bad(request):
        return httpx.Response(400, json={"error": "bad model"})
    inf = InferenceAdapter(token_limit=50_000, transport=httpx.MockTransport(bad))
    with pytest.raises(httpx.HTTPStatusError):
        await inf.chat("m", MSGS)
    assert inf.reserved_tokens() == 0 and len(inf.rejected) == 1
