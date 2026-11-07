"""
Public, no-login endpoints for visitors and judges.

  GET  /public/scenarios                      the four preset demos (plain-language)
  GET  /public/replays                        recorded real runs the operator chose to show
  POST /public/try {"scenario": "<id>"}       start a GUEST session (presets only, capped)
  GET  /public/sessions/{id}                  live/archived snapshot      } replay, or guest
  GET  /public/sessions/{id}/events           signed timeline events      } with X-Guest-Token
  GET  /public/sessions/{id}/evidence         full verification report    }
  GET  /public/sessions/{id}/evidence/export  signed ledger (.jsonl)      }
  GET  /public/sessions/{id}/evidence/public-key
  POST /public/sessions/{id}/evidence/tamper-test   (in-memory copy only)
  POST /public/sessions/{id}/pause|resume|kill      guest's OWN live session only

Guests can never send their own task text or budgets: only the server-side
presets in backend/scenarios.py run. Limits: GUEST_MAX_LIVE guest sessions at
once and GUEST_PER_IP_PER_HOUR per visitor, on top of the global usage caps.
"""
import asyncio
import hashlib
import hmac
import json
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from backend import scenarios, security
from backend.api import evidence as ev_api
from backend.api import governance as gov_api
from backend.api import sessions as sess_api
from backend.config import settings
from backend.orchestrator.inference_pricing import has_price
from backend.evidence.archive import read_entries, summarize
from backend.services.session_manager import LimitExceeded, SessionStartError, session_manager

router = APIRouter(prefix="/public", tags=["public"])

_guest_lock = asyncio.Lock()
_guest_starts: dict[str, deque] = defaultdict(deque)


# ------------------------------------------------------------------ featured replays

def _featured_path() -> Path:
    return Path(settings.ledger_dir) / "featured.json"


def featured() -> list[str]:
    try:
        ids = json.loads(_featured_path().read_text())
        return [i for i in ids if isinstance(i, str) and i.isalnum()]
    except Exception:
        return []


def set_featured(session_id: str, on: bool) -> list[str]:
    ids = [i for i in featured() if i != session_id]
    if on:
        ids.insert(0, session_id)
    p = _featured_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ids))
    return ids


# ------------------------------------------------------------------ guest tokens

def _guest_secret() -> bytes:
    # derived from the evidence signing key: stable across restarts, never sent anywhere
    return hashlib.sha256(b"agentlimit-guest-v1" + session_manager.signer._sk.encode()).digest()


def guest_token(session_id: str) -> str:
    return hmac.new(_guest_secret(), session_id.encode(), hashlib.sha256).hexdigest()


def _is_guest(session_id: str, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(token, guest_token(session_id))


def _require_view(session_id: str, token: str | None) -> None:
    if not session_id.isalnum():
        raise HTTPException(404, "not found")
    if session_id in featured() or _is_guest(session_id, token):
        return
    raise HTTPException(404, "not a public replay (ask the operator to publish it, or start your own run)")


def _require_guest_live(session_id: str, token: str | None) -> None:
    if not _is_guest(session_id, token):
        raise HTTPException(403, "you can only control a run you started")
    if not session_manager.get(session_id):
        raise HTTPException(409, "this run has already finished")


# ------------------------------------------------------------------ routes

@router.get("/scenarios")
async def list_scenarios():
    return {"scenarios": [scenarios.public_view(s) for s in scenarios.SCENARIOS],
            "guest_mode": settings.guest_mode_enabled,
            "guest_live_limit": settings.guest_max_live,
            "guest_per_hour": settings.guest_per_ip_per_hour}


@router.get("/replays")
async def list_replays():
    out = []
    for sid in featured():
        rt = session_manager.get(sid)
        entries = rt.ledger.read_all() if rt else read_entries(sid)
        if not entries:
            continue
        sm = summarize(entries)
        closed = next((e["payload"] for e in reversed(entries) if e["event_type"] == "session_closed"), None)
        kill = next((e["payload"] for e in reversed(entries)
                     if e["event_type"] in ("kill_verified", "kill_failed")), None)
        try:
            report = ev_api._report(sid)
        except Exception:
            report = None
        out.append({"session_id": sid, "task": sm["task"][:160], "final_state": sm["final_state"],
                    "agent_status": sm["agent_status"], "created_at": sm["created_at"],
                    "kill_verdict": (kill or {}).get("verdict"), "closed": bool(closed),
                    "artifact_verdict": ((report or {}).get("artifact_verification") or {}).get("verdict"),
                    "highlights": outcomes(entries, report),
                    "scenario": next((s["id"] for s in scenarios.SCENARIOS if s["task"] == sm["task"]), None)})
    return out


def outcomes(entries: list[dict], report: dict | None) -> list[dict]:
    """What a run actually demonstrated, derived ONLY from its signed record and the
    re-derived verification report, never from the scenario label."""
    out: list[dict] = []
    last = lambda t: next((e["payload"] for e in reversed(entries) if e["event_type"] == t), None)
    kill = next((e["payload"] for e in reversed(entries) if e["event_type"] in ("kill_verified", "kill_failed")), None)
    if kill:
        if kill.get("verdict") == "KILL_CONFIRMED" and (report or {}).get("verification", {}).get("termination") == "VALID":
            out.append({"signal": "red", "text": f"External stop confirmed ({sum(bool(v) for v in kill.get('checks', {}).values())}/{len(kill.get('checks', {}))} checks)"})
        else:
            out.append({"signal": "yellow", "text": "Stop NOT confirmed"})
    stopped = last("agent_stopped")
    if stopped and stopped.get("stopped_by") == "governor" and "retry" in str(stopped.get("reason", "")):
        out.append({"signal": "yellow", "text": "Repeated failing attempts stopped by AgentLimit"})
    for e in entries:
        p = e["payload"]
        if e["event_type"] == "recommendation_applied" and p.get("action") == "set_memory_high":
            out.append({"signal": "yellow", "text": "Memory squeeze applied and verified by readback" if p.get("verified")
                        else "Memory squeeze applied, NOT verified"})
            break
    thr = last("throttle_applied")
    if thr:
        down = isinstance(thr.get("model_downgrade"), dict)
        out.append({"signal": "orange", "text": ("Slowed down (verified)" if thr.get("verified") else "Slow-down NOT verified")
                    + (" + switched to a cheaper model" if down else "")})
    av = ((report or {}).get("artifact_verification") or {}).get("verdict")
    if av == "VERIFIED":
        out.insert(0, {"signal": "green", "text": "Answer independently re-calculated: correct"})
    elif av == "INTEGRITY_ONLY":
        out.insert(0, {"signal": "grey", "text": "Output sealed (answer not re-calculated for this task)"})
    elif av == "FAILED":
        out.insert(0, {"signal": "red", "text": "Answer did NOT match the independent calculation"})
    if report and report.get("verification", {}).get("ledger_integrity") == "VALID" \
            and report["verification"].get("ledger_completeness") == "COMPLETE":
        out.append({"signal": "green", "text": "Signed record valid and complete"})
    return out


class TryRequest(BaseModel):
    scenario: str


@router.post("/try")
async def try_it(req: TryRequest, request: Request):
    if not settings.guest_mode_enabled:
        raise HTTPException(403, "guest runs are switched off on this server; watch a replay instead")
    s = scenarios.BY_ID.get(req.scenario)
    if not s:
        raise HTTPException(422, "unknown scenario")
    ip = security.client_ip(request)
    now = time.time()
    async with _guest_lock:
        q = _guest_starts[ip]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= settings.guest_per_ip_per_hour:
            raise HTTPException(429, f"you have started {len(q)} runs in the last hour; "
                                     "please watch a replay or come back later")
        live = [rt for rt in session_manager.list() if getattr(rt, "guest", False) and not rt.closed]
        if len(live) >= settings.guest_max_live:
            raise HTTPException(429, "someone else is trying it right now; runs take 1-3 minutes. "
                                     "Watch a replay meanwhile and try again shortly")
        budget = sess_api.build_budget(sess_api.CreateSessionRequest(
            task=s["task"], cpu_budget_seconds=s["cpu"], memory_budget_mb=s["mem"],
            token_budget=s["tokens"], wall_clock_seconds=s["wall"],
            cost_limit_usd=s["cost"] if has_price(settings.vultr_model_default) else 0))
        try:
            rt = await session_manager.create(s["task"], budget)
        except LimitExceeded as e:
            raise HTTPException(429, f"the server's daily safety cap was reached: {e}")
        except SessionStartError as e:
            raise HTTPException(503, {"category": e.category, "session_id": e.session_id, "detail": e.detail})
        rt.guest = True
        q.append(now)
        rt.ledger.append("guest_session", {"scenario": s["id"], "note": "started from the public demo page"})
    await session_manager.start_run(rt.session_id)
    return {"session_id": rt.session_id, "guest_token": guest_token(rt.session_id), "scenario": s["id"]}


@router.get("/sessions/{session_id}")
async def public_session(session_id: str, x_guest_token: str | None = Header(None)):
    _require_view(session_id, x_guest_token)
    snap = await sess_api.get_session(session_id)
    if isinstance(snap, dict):
        snap["public_view"] = True
        snap["can_control"] = _is_guest(session_id, x_guest_token) and session_manager.get(session_id) is not None
        snap["scenario"] = next((s["id"] for s in scenarios.SCENARIOS if s["task"] == snap.get("task")), None)
    return snap


@router.get("/sessions/{session_id}/events")
async def public_events(session_id: str, x_guest_token: str | None = Header(None)):
    _require_view(session_id, x_guest_token)
    return await gov_api.list_events(session_id)


@router.get("/sessions/{session_id}/evidence")
async def public_evidence(session_id: str, x_guest_token: str | None = Header(None)):
    _require_view(session_id, x_guest_token)
    return await ev_api.get_evidence(session_id)


@router.get("/sessions/{session_id}/evidence/export")
async def public_export(session_id: str, g: str | None = None, x_guest_token: str | None = Header(None)):
    _require_view(session_id, x_guest_token or g)      # ?g= so a plain download link works
    return await ev_api.export_ledger(session_id)


@router.get("/sessions/{session_id}/evidence/public-key")
async def public_key(session_id: str, x_guest_token: str | None = Header(None)):
    _require_view(session_id, x_guest_token)
    return await ev_api.public_key(session_id)


@router.post("/sessions/{session_id}/evidence/tamper-test")
async def public_tamper(session_id: str, x_guest_token: str | None = Header(None)):
    _require_view(session_id, x_guest_token)
    return await ev_api.tamper_test(session_id)


@router.post("/sessions/{session_id}/pause")
async def public_pause(session_id: str, x_guest_token: str | None = Header(None)):
    _require_guest_live(session_id, x_guest_token)
    return await gov_api.pause(session_id)


@router.post("/sessions/{session_id}/resume")
async def public_resume(session_id: str, x_guest_token: str | None = Header(None)):
    _require_guest_live(session_id, x_guest_token)
    return await gov_api.resume(session_id, 0)       # guests can't extend budgets


@router.post("/sessions/{session_id}/kill")
async def public_kill(session_id: str, x_guest_token: str | None = Header(None)):
    _require_guest_live(session_id, x_guest_token)
    return await gov_api.kill(session_id)
