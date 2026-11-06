import asyncio

import pytest

from backend.evidence.ledger import EvidenceLedger
from backend.evidence.signer import Signer
from backend.governance.budget import SessionBudget
from backend.governance.interventions import InterventionController, KillVerdict, PauseMode
from backend.orchestrator.agent_loop import AgentOrchestrator
from backend.tests.fakes import FakeCgroup, FakeRuntime


@pytest.fixture
def parts(tmp_path):
    signer = Signer(str(tmp_path / "key"))
    ledger = EvidenceLedger("t-interv", signer, str(tmp_path / "ledger"))
    budget = SessionBudget(policy_id="p").validate()
    cg = FakeCgroup()
    orch = AgentOrchestrator("t-interv", "task", ledger, budget, runtime=FakeRuntime())
    ctrl = InterventionController("t-interv", cg, ledger, orch, budget, runtime=orch.runtime)
    return ctrl, cg, orch, ledger, budget


def events(ledger):
    return [e["event_type"] for e in ledger.read_all()]


@pytest.mark.asyncio
async def test_throttle_writes_cpu_max_and_downgrades_model(parts):
    ctrl, cg, orch, ledger, _ = parts
    await ctrl.throttle(cpu_quota_usec=50_000)
    assert cg.cpu_max().startswith("50000")
    assert orch.inference._throttled is True
    assert "throttle_applied" in events(ledger)


@pytest.mark.asyncio
async def test_pause_freezes_and_holds_orchestrator(parts):
    ctrl, cg, orch, _, _ = parts
    mode = await ctrl.pause()
    assert mode == PauseMode.KERNEL_FREEZE and cg.frozen
    assert not orch._resume_event.is_set()   # host loop held too


@pytest.mark.asyncio
async def test_pause_falls_back_to_step_boundary(parts, monkeypatch):
    ctrl, cg, orch, _, _ = parts
    monkeypatch.setattr(cg, "freeze", lambda timeout_s=2.0: False)
    assert await ctrl.pause() == PauseMode.STEP_BOUNDARY
    assert not orch._resume_event.is_set()
    assert await ctrl.resume()
    assert orch._resume_event.is_set()


@pytest.mark.asyncio
async def test_resume_extends_budget_and_restores_quota(parts):
    ctrl, cg, _, ledger, budget = parts
    await ctrl.throttle()
    await ctrl.pause()
    await ctrl.resume(extra_cpu_usec=15_000_000)
    assert budget.cpu_budget_usec == 45_000_000
    assert cg.cpu_max().startswith("100000") and not cg.frozen
    assert "budget_extension" in events(ledger)


@pytest.mark.asyncio
async def test_kill_terminates_agent_inference_and_sandbox(parts):
    ctrl, cg, orch, ledger, _ = parts

    async def long_running():
        await asyncio.sleep(30)
    task = asyncio.create_task(long_running())
    orch.attach_task(task)

    result = await ctrl.kill(reason="test")
    assert result["verdict"] == KillVerdict.KILL_CONFIRMED.value
    assert all(result["checks"].values()), result["checks"]
    assert orch.terminated and orch.inference.revoked and task.done()
    assert orch.runtime.stopped
    assert "kill_verified" in events(ledger)


@pytest.mark.asyncio
async def test_kill_reports_failure_when_workload_survives(parts):
    ctrl, cg, orch, ledger, _ = parts
    cg.kill_works = False
    result = await ctrl.kill(reason="test")
    assert result["verdict"] == KillVerdict.KILL_FAILED.value
    assert result["checks"]["cgroup_populated_0"] is False
    assert "kill_failed" in events(ledger) and "kill_verified" not in events(ledger)


@pytest.mark.asyncio
async def test_memory_high_recommendation_applies(parts):
    ctrl, cg, _, _, _ = parts
    _, _, _, _, budget = parts
    assert await ctrl.apply_recommendation({"action": "set_memory_high", "rule_id": "memory_pressure"})
    # memory.high is computed on the SAME boundary as the policy budget and sits below it
    assert cg.memory_high == int(0.90 * budget.memory_limit_bytes) < budget.memory_limit_bytes


@pytest.mark.asyncio
async def test_kill_fails_when_runtime_cannot_remove_sandbox(parts):
    ctrl, cg, orch, ledger, _ = parts
    orch.runtime.stop_fails = True
    result = await ctrl.kill(reason="test")
    assert result["verdict"] == KillVerdict.KILL_FAILED.value
    assert result["checks"]["sandbox_removed_via_runtime"] is False
    assert "runtime_stop_error" in events(ledger)


@pytest.mark.asyncio
async def test_throttle_does_not_claim_model_downgrade_without_validated_cheap_model(parts, monkeypatch):
    from backend.config import settings
    from backend.orchestrator import inference_pricing as pricing, model_registry
    ctrl, cg, orch, ledger, _ = parts
    monkeypatch.setattr(settings, "vultr_model_cheap", "")
    model_registry.validate([settings.vultr_model_default])
    await ctrl.throttle()
    ev = [e for e in ledger.read_all() if e["event_type"] == "throttle_applied"][-1]["payload"]
    assert "unavailable" in ev["model_downgrade"] and "CPU throttling only" in ev["model_downgrade"]
    assert orch.inference._effective_model(settings.vultr_model_default) == settings.vultr_model_default
    assert ev["applied"] is True and ev["cpu_max_readback"].startswith("50000")
