import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.evidence.archive import list_archived, read_entries, summarize
from backend.governance.budget import SessionBudget
from backend.config import settings
from backend.orchestrator.inference_pricing import has_price
from backend.services.session_manager import LimitExceeded, SessionStartError, session_manager

router = APIRouter(prefix="/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    task: str = Field(min_length=1, max_length=4000)
    cpu_budget_seconds: int = Field(30, ge=1, le=3600)
    memory_budget_mb: int = Field(512, ge=320, le=4096)   # host-accounted VMM memory
    wall_clock_seconds: int = Field(300, ge=10, le=3600)
    token_budget: int = Field(50_000, ge=1000, le=2_000_000)
    cost_limit_usd: float = Field(0.0, ge=0)
    alert_pct: int = 70
    recommend_pct: int = 80
    throttle_pct: int = 85
    pause_pct: int = 95


class CreateSessionResponse(BaseModel):
    session_id: str
    status: str
    runtime_type: str
    containment: dict


def build_budget(req: CreateSessionRequest) -> SessionBudget:
    """Every field is set explicitly — no ORM column defaults involved."""
    return SessionBudget(
        policy_id=uuid.uuid4().hex[:12],
        cpu_budget_usec=req.cpu_budget_seconds * 1_000_000,
        memory_limit_bytes=req.memory_budget_mb * 1024 * 1024,
        wall_clock_limit_seconds=req.wall_clock_seconds,
        token_limit=req.token_budget,
        cost_limit_usd=req.cost_limit_usd,
        alert_pct=req.alert_pct,
        recommend_pct=req.recommend_pct,
        throttle_pct=req.throttle_pct,
        pause_pct=req.pause_pct,
    ).validate()


@router.post("", response_model=CreateSessionResponse)
async def create_session(req: CreateSessionRequest):
    try:
        budget = build_budget(req)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if budget.cost_limit_usd > 0 and not has_price(settings.vultr_model_default):
        raise HTTPException(422, f"a dollar cap needs verified pricing for {settings.vultr_model_default!r} "
                                 "(backend/pricing.json); set the cap to 0 or add the price")
    try:
        rt = await session_manager.create(req.task, budget)
    except LimitExceeded as e:
        raise HTTPException(429, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except SessionStartError as e:
        raise HTTPException(503, {"category": e.category, "session_id": e.session_id, "detail": e.detail,
                                  "evidence": f"/sessions/{e.session_id}/evidence"})
    await session_manager.start_run(rt.session_id)
    return CreateSessionResponse(session_id=rt.session_id, status="running",
                                 runtime_type=rt.runtime_type, containment=rt.containment)


@router.get("")
async def list_sessions():
    live = {rt.session_id: rt for rt in session_manager.list()}
    out = [{"session_id": rt.session_id, "task": rt.task[:120], "state": rt.engine.state.value,
            "agent_status": rt.orchestrator.status, "created_at": rt.created_at, "closed": rt.closed,
            "archived": False} for rt in reversed(list(live.values()))]
    for sid in list_archived()[:50]:
        if sid in live:
            continue
        sm = summarize(read_entries(sid) or [])
        out.append({"session_id": sid, "task": sm["task"][:120], "state": sm["final_state"],
                    "agent_status": sm["agent_status"], "created_at": sm["created_at"], "closed": True,
                    "archived": True})
    return out


def _archived_snapshot(session_id: str):
    entries = read_entries(session_id)
    if entries is None:
        raise HTTPException(404, "session not found")
    sm = summarize(entries)
    probe = next((e["payload"] for e in reversed(entries) if e["event_type"] == "containment_probe"), {})
    sample = next((e["payload"] for e in reversed(entries) if e["event_type"] == "resource_sample"), {})
    fm = (sm["lifecycle"] or {}).get("final_metrics") or {}
    if fm:
        sample = {**sample, "cpu_usage_usec": fm.get("cpu_usec", sample.get("cpu_usage_usec", 0)),
                  "memory_current_bytes": fm.get("memory_bytes", 0),
                  "memory_peak_bytes": fm.get("memory_peak_bytes", sample.get("memory_peak_bytes", 0)),
                  "wall_seconds": round(fm.get("wall_seconds", sample.get("wall_seconds", 0)), 2)}
    attach = next((e["payload"] for e in reversed(entries) if e["event_type"] == "sandbox_attached"), {})
    rt_ev = next((e["payload"] for e in reversed(entries) if e["event_type"] == "runtime_started"), {})
    inf = sm["inference"] or {"input_tokens": 0, "output_tokens": 0, "total_tokens": sample.get("token_spend", 0),
                              "estimated_cost_usd": 0.0, "cost_usd": 0.0, "throttled": False, "last_model": ""}
    return {
        "session_id": session_id, "task": sm["task"], "state": sm["final_state"],
        "agent_status": sm["agent_status"], "steps": sm["steps"],
        "cpu_usec": sample.get("cpu_usage_usec", 0), "memory_bytes": sample.get("memory_current_bytes", 0),
        "memory_peak_bytes": sample.get("memory_peak_bytes", 0), "tokens": inf.get("total_tokens", 0),
        "wall_seconds": sample.get("wall_seconds", 0), "utilisation_pct": sample.get("utilisation_pct", {}),
        "runtime_type": rt_ev.get("runtime_type", ""), "sandbox_pid": attach.get("pid"),
        "memory_telemetry_valid": sample.get("memory_telemetry_valid", False), "inference": inf,
        "containment": probe, "termination": sm["termination"],
        "lifecycle": sm["lifecycle"] or {"agent_status": sm["agent_status"], "interrupted": sm["interrupted"]},
        "recommendations": [e["payload"] for e in entries if e["event_type"] == "recommendation_issued"],
        "agent_claim": None, "budget": sm["budget"], "archived": True,
    }


@router.get("/{session_id}")
async def get_session(session_id: str):
    rt = session_manager.get(session_id)
    if not rt:
        return _archived_snapshot(session_id)
    eng, orch = rt.engine, rt.orchestrator
    fm = eng._frozen or {}
    return {
        "session_id": session_id,
        "task": rt.task,
        "state": eng.state.value,
        "agent_status": orch.status,
        "steps": orch.step_count,
        "cpu_usec": fm.get("cpu_usec", rt.cgroup.cpu_usage_usec()),
        "memory_bytes": fm.get("memory_bytes", rt.cgroup.memory_current()),
        "memory_peak_bytes": fm.get("memory_peak_bytes", rt.cgroup.memory_peak()),
        "tokens": orch.inference.total_tokens(),
        "wall_seconds": round(eng.wall_seconds, 2),
        "utilisation_pct": {k: round(v, 1) for k, v in eng.last_utilisation.items()},
        "runtime_type": rt.runtime_type,
        "sandbox_pid": rt.sandbox_pid,
        "memory_telemetry_valid": eng._memory_telemetry_valid,
        "inference": orch.inference.snapshot(),
        "containment": rt.containment,
        "memory_plan": rt.memory,
        "termination": eng.termination,
        "lifecycle": rt.lifecycle,
        "recommendations": eng.recommendations,
        "agent_claim": (orch.final_output or {}).get("stdout", "")[:2000] or None,
        "budget": rt.budget.to_dict(),
        "archived": False,
    }
