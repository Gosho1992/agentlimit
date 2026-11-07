import asyncio

from fastapi import APIRouter, HTTPException

from backend.services.session_manager import session_manager

router = APIRouter(prefix="/sessions", tags=["governance"])

TIMELINE_EVENTS = {
    "session_created", "inputs_snapshotted", "cgroup_configured", "runtime_started",
    "sandbox_attached", "containment_probe", "governance_started", "state_transition",
    "throttle_applied", "pause_applied", "resume_applied", "kill_requested", "kill_verified",
    "kill_failed", "orchestrator_terminated", "recommendation_issued", "recommendation_applied",
    "manual_pause", "manual_resume", "manual_kill", "budget_extension", "agent_completed",
    "agent_stopped", "agent_budget_exceeded", "agent_cancelled", "memory_telemetry_validated",
    "session_closed", "governance_tick_error", "pause_timeout_escalation", "session_interrupted",
    "runtime_stop_error", "inference_call", "empty_generation", "session_start_failed",
    "sandbox_attach_failed", "watchdog_kill", "recommendation_delivery_failed", "kill_retry", "guest_session",
}


def _rt(session_id: str):
    rt = session_manager.get(session_id)
    if not rt:
        raise HTTPException(409, "session is archived (read-only) or does not exist")
    return rt


@router.get("/{session_id}/events")
async def list_events(session_id: str):
    from backend.evidence.archive import read_entries
    rt = session_manager.get(session_id)
    entries = rt.ledger.read_all() if rt else read_entries(session_id)
    if entries is None:
        raise HTTPException(404, "session not found")
    return [e for e in entries if e["event_type"] in TIMELINE_EVENTS]


@router.post("/{session_id}/pause")
async def pause(session_id: str):
    rt = _rt(session_id)
    if rt.closed:
        raise HTTPException(409, "session already closed")
    rt.engine.force_pause()
    # the state flips to PAUSE before the kernel freeze finishes: wait for the intervention's own result
    for _ in range(100):
        if rt.engine.state.value == "KILL" or rt.engine.interventions.paused_mode is not None:
            break
        await asyncio.sleep(0.05)
    mode = rt.engine.interventions.paused_mode
    frozen = bool(mode and mode.value == "KERNEL_FREEZE" and rt.cgroup.is_frozen())
    return {"paused": rt.engine.state.value == "PAUSE" and mode is not None, "state": rt.engine.state.value,
            "mode": mode.value if mode else None, "verified_frozen": frozen}


@router.post("/{session_id}/resume")
async def resume(session_id: str, extra_cpu_seconds: int = 0):
    rt = _rt(session_id)
    if rt.closed:
        raise HTTPException(409, "session already closed")
    return await rt.engine.resume(max(0, extra_cpu_seconds) * 1_000_000)


@router.post("/{session_id}/kill")
async def kill(session_id: str):
    """Returns killed=true ONLY when every termination check is verified.
    A closed run is final: 409, and nothing is changed or recorded."""
    rt = _rt(session_id)
    if rt.closed or rt.ledger.sealed:
        raise HTTPException(409, "Run already closed")
    result = rt.engine.termination
    if not result:
        result = await rt.engine.kill_now("operator_kill")
    elif result.get("verdict") != "KILL_CONFIRMED" and not rt.closed:
        result = await rt.engine.retry_kill()          # a failed stop can be retried, never undone
    confirmed = result.get("verdict") == "KILL_CONFIRMED"
    return {
        "killed": confirmed,
        "verdict": result.get("verdict"),
        "checks": result.get("checks"),
        "state": rt.engine.state.value,
        "workload_gone": not rt.cgroup.is_populated(),
    }
