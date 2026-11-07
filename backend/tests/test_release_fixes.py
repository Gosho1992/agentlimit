"""Regression tests for the live-QA release blockers (G1, G2) and follow-ups."""
import asyncio

import httpx
import pytest

from backend.evidence.ledger import EvidenceLedger, LedgerSealedError, TERMINAL_EVENT_TYPES, verify_entries
from backend.evidence.signer import Signer
from backend.tests.test_session_api import app_env, wait_closed  # noqa: F401  (fixture)

AUTH = {"Authorization": "Bearer test-token-123"}


def _client(app, **kw):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 1)),
                             base_url="http://t", **kw)


@pytest.fixture(autouse=True)
def _fresh_guest_limits():
    from backend.api import public
    public._guest_starts.clear()
    yield
    public._guest_starts.clear()


# ------------------------------------------------------------------ G1: ledger immutability

@pytest.mark.parametrize("terminal", TERMINAL_EVENT_TYPES)
def test_no_event_can_be_appended_after_a_terminal_event(tmp_path, terminal):
    s = Signer(str(tmp_path / "k"))
    led = EvidenceLedger("x", s, str(tmp_path))
    led.append("session_created", {})
    led.append(terminal, {})
    before = led.path.read_bytes()
    with pytest.raises(LedgerSealedError):
        led.append("manual_kill", {})
    assert led.path.read_bytes() == before                        # nothing written
    reopened = EvidenceLedger("x", s, str(tmp_path))               # seal survives a restart
    assert reopened.sealed
    with pytest.raises(LedgerSealedError):
        reopened.append("state_transition", {})
    assert verify_entries(reopened.read_all(), s.public_key_b64())[0]


def test_unsealed_ledger_still_appends(tmp_path):
    led = EvidenceLedger("x", Signer(str(tmp_path / "k")), str(tmp_path))
    led.append("session_created", {})
    led.append("kill_verified", {})
    assert not led.sealed and len(led.read_all()) == 2


# ------------------------------------------------------------------ G1: kill on a closed run

async def _finished_run(c):
    sid = (await c.post("/sessions", json={"task": "reconcile"})).json()["session_id"]
    await wait_closed(c, sid)
    return sid


def _ledger_state(sid):
    from backend.evidence.archive import read_entries
    e = read_entries(sid)
    return len(e), e[-1]["entry_hash"], e[-1]["event_type"]


@pytest.mark.asyncio
async def test_kill_on_closed_run_is_409_and_changes_nothing(app_env):
    app, _ = app_env
    async with _client(app, headers=AUTH) as op:
        sid = await _finished_run(op)
        before_snap = (await op.get(f"/sessions/{sid}")).json()
        before_ev = (await op.get(f"/sessions/{sid}/evidence")).json()
        before_ledger = _ledger_state(sid)
        r = await op.post(f"/sessions/{sid}/kill")
        assert r.status_code == 409 and r.json()["detail"] == "Run already closed"
        assert _ledger_state(sid) == before_ledger                  # same count, same final hash
        after_snap = (await op.get(f"/sessions/{sid}")).json()
        assert after_snap["state"] == before_snap["state"] == "NORMAL"
        assert after_snap["agent_status"] == "completed" and after_snap["termination"] is None
        after_ev = (await op.get(f"/sessions/{sid}/evidence")).json()
        assert after_ev["verification"] == before_ev["verification"]
        assert after_ev["verification"]["ledger_completeness"] == "COMPLETE"
        assert after_ev["verification"]["termination"] == "NOT_APPLICABLE"


@pytest.mark.asyncio
async def test_guest_kill_on_own_closed_run_is_409(app_env):
    app, _ = app_env
    async with _client(app) as anon:
        r = (await anon.post("/public/try", json={"scenario": "reconcile"})).json()
        sid, g = r["session_id"], {"X-Guest-Token": r["guest_token"]}
        for _ in range(100):
            if (await anon.get(f"/public/sessions/{sid}", headers=g)).json().get("lifecycle"):
                break
            await asyncio.sleep(0.05)
        before = _ledger_state(sid)
        k = await anon.post(f"/public/sessions/{sid}/kill", headers=g)
        assert k.status_code == 409 and k.json()["detail"] == "Run already closed"
        assert _ledger_state(sid) == before
        s = (await anon.get(f"/public/sessions/{sid}", headers=g)).json()
        assert s["state"] == "NORMAL" and s["agent_status"] == "completed"


@pytest.mark.asyncio
async def test_kill_on_active_run_still_confirmed(app_env):
    app, holder = app_env
    from backend.tests.fakes import FakeRuntime
    holder["runtime"] = FakeRuntime([{"stdout": "", "stderr": "", "exit_code": 0}], delay=30)
    async with _client(app, headers=AUTH) as op:
        sid = (await op.post("/sessions", json={"task": "spin"})).json()["session_id"]
        await asyncio.sleep(0.2)
        k = (await op.post(f"/sessions/{sid}/kill")).json()
        assert k["verdict"] == "KILL_CONFIRMED" and k["killed"] is True
        await wait_closed(op, sid)
        assert _ledger_state(sid)[2] == "session_closed"            # still sealed properly after the kill
        again = await op.post(f"/sessions/{sid}/kill")
        assert again.status_code == 409


# ------------------------------------------------------------------ G2: memory policy reachability

def test_memory_thresholds_are_reachable_for_every_allowed_budget():
    """Old policy (guest = budget - 192 MB) made 80 % of the budget unreachable below 960 MB."""
    from backend.governance.resource_policy import memory_plan
    for mb in range(320, 4097, 32):
        p = memory_plan(mb * 2**20)
        guest = p["guest_ram_mb"] * 2**20
        assert guest > p["recommend_threshold_bytes"], mb          # the 80 % intervention can happen
        assert guest < p["host_memory_max_bytes"], mb              # guest RAM still below the hard ceiling
        assert p["vmm_headroom_mb"] >= 48


def test_memory_preset_is_bounded_by_the_guest_not_by_luck():
    from backend.scenarios import BY_ID
    s = BY_ID["memory"]
    assert "MemAvailable" in s["task"] and "sleep" in s["task"]      # stops before the guest runs out, holds for sampling
    assert s["mem"] >= 1024


# ------------------------------------------------------------------ follow-ups

def test_long_output_keeps_head_and_tail():
    from backend.orchestrator.agent_loop import _clip
    text = "Traceback (most recent call last):\n" + "x" * 5000 + "\nurllib.error.URLError: <urlopen error>"
    out = _clip(text)
    assert out.startswith("Traceback") and out.endswith("URLError: <urlopen error>")
    assert "chars omitted" in out and len(out) < 2100
    assert _clip("short") == "short"


@pytest.mark.asyncio
async def test_security_headers_on_every_response(app_env):
    app, _ = app_env
    async with _client(app) as anon:
        for path in ("/", "/health", "/public/scenarios", "/sessions"):
            h = (await anon.get(path)).headers
            assert h["x-frame-options"] == "DENY", path
            assert h["x-content-type-options"] == "nosniff", path
            assert "frame-ancestors 'none'" in h["content-security-policy"], path
            assert h["referrer-policy"] == "no-referrer", path
            assert "max-age=" in h["strict-transport-security"], path


@pytest.mark.asyncio
async def test_memory_high_is_page_aligned_so_kernel_readback_verifies(tmp_path):
    """Live run 10e741415bdf42f3: wrote 1449551462, kernel read back 1449549824 (page-rounded) -> verified=false."""
    from backend.governance.resource_policy import memory_plan
    from backend.tests.test_engine import make
    eng, orch, cg, ledger, *_ = make(tmp_path)
    eng.interventions.memory_plan = memory_plan(1536 * 2**20)
    assert eng.interventions.memory_plan["memory_high_bytes"] % 4096 == 0
    assert await eng.interventions.apply_recommendation({"action": "set_memory_high", "rule_id": "memory_pressure"})
    p = [e for e in ledger.read_all() if e["event_type"] == "recommendation_applied"][-1]["payload"]
    assert p["memory.high_bytes"] % 4096 == 0
    assert p["memory.high_readback"] == str(p["memory.high_bytes"]) and p["verified"] is True
