"""End-to-end governor tests with real budget objects, the real engine,
interventions, orchestrator and inference adapter (mock HTTP transport)."""
import asyncio

import pytest

from backend.api.sessions import CreateSessionRequest, build_budget
from backend.evidence.ledger import EvidenceLedger
from backend.evidence.signer import Signer
from backend.governance.engine import GovernanceEngine
from backend.governance.thresholds import State
from backend.orchestrator.agent_loop import AgentOrchestrator
from backend.orchestrator.inference_adapter import InferenceAdapter
from backend.tests.fakes import FakeCgroup, FakeRuntime, mock_inference_transport


def make(tmp_path, runtime=None, transport=None, **budget_kw):
    signer = Signer(str(tmp_path / "key"))
    ledger = EvidenceLedger("t-eng", signer, str(tmp_path / "ledger"))
    budget = build_budget(CreateSessionRequest(task="t", **budget_kw))
    cg = FakeCgroup()
    inf = InferenceAdapter(token_limit=budget.token_limit, transport=transport or mock_inference_transport())
    orch = AgentOrchestrator("t-eng", "task", ledger, budget, runtime=runtime or FakeRuntime(), inference=inf,
                             artifact_dir=tmp_path / "art")
    eng = GovernanceEngine("t-eng", cg, ledger, budget, orch, poll_interval_s=0.01,
                           token_spend_fn=orch.token_spend, runtime=orch.runtime, pause_timeout_s=0.3)
    orch.pre_step_gate = eng.pre_step_check
    return eng, orch, cg, ledger, budget, signer


def transitions(ledger):
    return [(e["payload"]["from"], e["payload"]["to"]) for e in ledger.read_all()
            if e["event_type"] == "state_transition"]


@pytest.mark.asyncio
async def test_ladder_walks_every_stage_with_real_budget_then_verified_kill(tmp_path):
    eng, orch, cg, ledger, budget, signer = make(tmp_path)
    cg.cpu = 29_000_000          # 96.7% in one jump: engine must still walk every stage
    agent = asyncio.create_task(asyncio.sleep(30))
    orch.attach_task(agent)
    for _ in range(4):
        await eng._tick()
    assert eng.state == State.PAUSE
    assert [t[1] for t in transitions(ledger)] == ["ALERT", "RECOMMEND", "THROTTLE", "PAUSE"]
    assert cg.frozen and cg.cpu_max().startswith("50000")
    cg.cpu = 31_000_000          # hard limit
    cg.frozen = False
    await eng._tick()
    assert eng.state == State.KILL
    t = eng.termination
    assert t["verdict"] == "KILL_CONFIRMED", t
    assert orch.inference.revoked and agent.done() and not cg.populated
    assert eng.tick_errors == 0
    assert ledger.verify_chain(signer.public_key_b64())[0]


@pytest.mark.asyncio
async def test_engine_loop_runs_without_tick_errors(tmp_path):
    """Regression for the None-threshold TypeError that silently disabled governance."""
    eng, *_ = make(tmp_path)
    task = asyncio.create_task(eng.run())
    await asyncio.sleep(0.1)
    eng.stop_event.set()
    await task
    assert eng.ticks > 3 and eng.tick_errors == 0


@pytest.mark.asyncio
async def test_kill_mid_inference_stops_further_model_calls(tmp_path):
    calls = []
    transport = mock_inference_transport(code="import time; time.sleep(1)", calls=calls, delay=0.3)
    eng, orch, cg, ledger, *_ = make(tmp_path, transport=transport,
                                     runtime=FakeRuntime([{"stdout": "", "stderr": "boom", "exit_code": 1}]))
    agent = asyncio.create_task(orch.run())
    orch.attach_task(agent)
    await asyncio.sleep(0.1)                 # first model call in flight
    result = await eng.kill_now("operator_kill")
    assert result["verdict"] == "KILL_CONFIRMED", result
    n = len(calls)
    await asyncio.sleep(0.6)
    assert len(calls) == n                   # no new inference after kill
    assert orch.status == "killed"


@pytest.mark.asyncio
async def test_resume_deescalates_state(tmp_path):
    eng, orch, cg, ledger, budget, _ = make(tmp_path)
    eng.force_pause()
    await eng._tick()
    assert eng.state == State.PAUSE
    r = await eng.resume()
    assert r["state"] == "NORMAL" and eng.state == State.NORMAL and not cg.frozen
    assert ("PAUSE", "NORMAL") in transitions(ledger)


@pytest.mark.asyncio
async def test_resume_with_extension_lowers_utilisation(tmp_path):
    eng, orch, cg, ledger, budget, _ = make(tmp_path)
    cg.cpu = 29_000_000
    for _ in range(4):
        await eng._tick()
    assert eng.state == State.PAUSE
    r = await eng.resume(extra_cpu_usec=30_000_000)   # 29/60 = 48%
    assert r["state"] == "NORMAL"
    await eng._tick()
    assert eng.state == State.NORMAL


@pytest.mark.asyncio
async def test_pause_timeout_escalates_to_kill(tmp_path):
    eng, orch, cg, *_ = make(tmp_path)
    eng.force_pause()
    await eng._tick()
    await asyncio.sleep(0.35)
    await eng._tick()
    assert eng.state == State.KILL and eng.termination["verdict"] == "KILL_CONFIRMED"


@pytest.mark.asyncio
async def test_governor_stops_retry_loop_before_agent_backstop(tmp_path):
    same_error = {"stdout": "", "stderr": "ZeroDivisionError: division by zero", "exit_code": 1}
    eng, orch, cg, ledger, *_ = make(tmp_path, runtime=FakeRuntime([same_error]))
    result = await orch.run()
    assert result is None
    ev = [e for e in ledger.read_all()]
    stopped = [e for e in ev if e["event_type"] == "agent_stopped"][0]["payload"]
    assert stopped["stopped_by"] == "governor"
    assert stopped["consecutive_identical_errors"] == 3      # governor acts at 3, agent's own limit is 5
    recs = [e["payload"]["rule_id"] for e in ev if e["event_type"] == "recommendation_issued"]
    assert "retry_loop" in recs
    assert orch.runtime.execs == 3


@pytest.mark.asyncio
async def test_token_rule_uses_per_step_deltas(tmp_path):
    eng, orch, *_ = make(tmp_path)
    orch._steps = [500, 520]
    orch.tokens_median = 510
    orch.tokens_last_step = 2000
    snap = eng._snapshot()
    assert snap["tokens_last_step"] == 2000 and snap["tokens_median"] == 510
    rules = {r.rule_id for r in eng.recommender.evaluate(snap)}
    assert "context_growth" in rules
    orch.tokens_last_step = 600
    assert "context_growth" not in {r.rule_id for r in eng.recommender.evaluate(eng._snapshot())}


@pytest.mark.asyncio
async def test_successful_run_records_artifact(tmp_path):
    eng, orch, cg, ledger, *_ = make(tmp_path, runtime=FakeRuntime([{"stdout": '{"a": 1}\n', "stderr": "", "exit_code": 0}]))
    out = await orch.run()
    assert out and out["stdout_sha256"]
    done = [e for e in ledger.read_all() if e["event_type"] == "agent_completed"][0]["payload"]
    assert "artifact_path" not in done                       # no absolute host path in the signed record
    assert done["artifact_relative_path"].startswith("artifacts/")
    assert (tmp_path / "art" / done["artifact_relative_path"].split("/", 1)[1]).read_text() == '{"a": 1}\n'


@pytest.mark.asyncio
async def test_empty_model_reply_is_retried_not_executed(tmp_path):
    import httpx, json
    replies = iter([{"choices": [{"message": {"content": "", "reasoning_content": "thinking..."},
                                  "finish_reason": "length"}], "usage": {"prompt_tokens": 100, "completion_tokens": 1500}},
                    {"choices": [{"message": {"content": "```python\nprint('{\"a\": 1}')\n```"}}],
                     "usage": {"prompt_tokens": 120, "completion_tokens": 50}}])

    async def handler(request):
        return httpx.Response(200, json=next(replies))
    rt = FakeRuntime([{"stdout": '{"a": 1}\n', "stderr": "", "exit_code": 0}])
    eng, orch, cg, ledger, *_ = make(tmp_path, runtime=rt, transport=httpx.MockTransport(handler))
    out = await orch.run()
    assert out and out["stdout"].strip() == '{"a": 1}'
    assert rt.execs == 1                                    # the empty reply was never executed
    assert any(e["event_type"] == "empty_generation" for e in ledger.read_all())


@pytest.mark.asyncio
async def test_exit_zero_with_no_output_is_not_completion(tmp_path):
    rt = FakeRuntime([{"stdout": "", "stderr": "", "exit_code": 0}])
    eng, orch, cg, ledger, *_ = make(tmp_path, runtime=rt)
    out = await orch.run()
    assert out is None and orch.status != "completed"
    assert not any(e["event_type"] == "agent_completed" for e in ledger.read_all())
