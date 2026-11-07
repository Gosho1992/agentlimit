"""Public demo surface: replays only for sessions the operator published,
guest runs only for server-side presets, guests control only their own run."""
import asyncio

import httpx
import pytest

from backend.tests.test_session_api import app_env, wait_closed  # noqa: F401  (fixture)

AUTH = {"Authorization": "Bearer test-token-123"}


@pytest.fixture(autouse=True)
def _fresh_guest_limits():
    from backend.api import public
    public._guest_starts.clear()
    yield
    public._guest_starts.clear()


def client(app, **kw):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.7", 1)),
                             base_url="http://t", **kw)


@pytest.mark.asyncio
async def test_scenarios_are_public_and_from_server(app_env):
    app, _ = app_env
    async with client(app) as c:
        r = (await c.get("/public/scenarios")).json()
        assert {s["id"] for s in r["scenarios"]} == {"reconcile", "runaway", "retry", "memory"}
        assert all(s["plain"] and s["expect"] for s in r["scenarios"])


@pytest.mark.asyncio
async def test_unpublished_session_is_not_public_until_featured(app_env):
    app, _ = app_env
    async with client(app, headers=AUTH) as op, client(app) as anon:
        sid = (await op.post("/sessions", json={"task": "reconcile"})).json()["session_id"]
        await wait_closed(op, sid)
        assert (await anon.get(f"/public/sessions/{sid}")).status_code == 404
        assert (await anon.get(f"/public/sessions/{sid}/evidence")).status_code == 404
        assert (await anon.post(f"/sessions/{sid}/feature")).status_code == 401      # operator only
        assert (await op.post(f"/sessions/{sid}/feature")).json()["featured"] is True
        assert any(r["session_id"] == sid for r in (await anon.get("/public/replays")).json())
        s = (await anon.get(f"/public/sessions/{sid}")).json()
        assert s["public_view"] and s["can_control"] is False
        ev = (await anon.get(f"/public/sessions/{sid}/evidence")).json()
        assert ev["verification"]["ledger_completeness"] == "COMPLETE"
        t = (await anon.post(f"/public/sessions/{sid}/evidence/tamper-test")).json()
        assert t["tamper_detected"] and t["truncation_detected"]
        assert (await anon.get(f"/public/sessions/{sid}/evidence/export")).status_code == 200
        assert (await anon.post(f"/public/sessions/{sid}/kill")).status_code == 403    # viewers can't control
        await op.post(f"/sessions/{sid}/feature?on=false")
        assert (await anon.get(f"/public/sessions/{sid}")).status_code == 404


@pytest.mark.asyncio
async def test_guest_try_runs_preset_with_scoped_token(app_env):
    app, _ = app_env
    async with client(app) as anon:
        r = await anon.post("/public/try", json={"scenario": "reconcile"})
        assert r.status_code == 200, r.text
        sid, tok = r.json()["session_id"], r.json()["guest_token"]
        g = {"X-Guest-Token": tok}
        s = (await anon.get(f"/public/sessions/{sid}", headers=g)).json()
        assert s["task"].startswith("Read /workspace/orders.csv")       # server-side preset text
        assert (await anon.get(f"/public/sessions/{sid}")).status_code == 404            # no token, not featured
        assert (await anon.get(f"/public/sessions/{sid}", headers={"X-Guest-Token": "0" * 64})).status_code == 404
        assert (await anon.get(f"/sessions/{sid}")).status_code == 401                   # operator API stays closed
        for _ in range(100):
            s = (await anon.get(f"/public/sessions/{sid}", headers=g)).json()
            if s.get("lifecycle"):
                break
            await asyncio.sleep(0.05)
        assert s["agent_status"] == "completed"
        ev = (await anon.get(f"/public/sessions/{sid}/evidence", headers=g)).json()
        assert ev["artifact_verification"]["verdict"] == "VERIFIED"


@pytest.mark.asyncio
async def test_guest_cannot_send_own_task_and_unknown_scenario_rejected(app_env):
    app, _ = app_env
    async with client(app) as anon:
        assert (await anon.post("/public/try", json={"scenario": "rm -rf"})).status_code == 422
        r = await anon.post("/public/try", json={"scenario": "reconcile", "task": "steal keys"})
        assert r.status_code == 200
        sid, tok = r.json()["session_id"], r.json()["guest_token"]
        s = (await anon.get(f"/public/sessions/{sid}", headers={"X-Guest-Token": tok})).json()
        assert "steal" not in s["task"]


@pytest.mark.asyncio
async def test_guest_limits_live_and_per_ip(app_env, monkeypatch):
    from backend.config import settings
    app, holder = app_env
    monkeypatch.setattr(settings, "guest_per_ip_per_hour", 2)
    import backend.services.session_manager as sm
    from types import SimpleNamespace
    mgr = sm.session_manager
    real_list = mgr.list
    busy = SimpleNamespace(guest=True, closed=False)
    async with client(app) as anon:
        monkeypatch.setattr(mgr, "list", lambda: real_list() + [busy])      # another visitor's live run
        b = await anon.post("/public/try", json={"scenario": "reconcile"})
        assert b.status_code == 429 and "someone else" in b.json()["detail"]
        busy.closed = True
        for _ in range(2):
            r = await anon.post("/public/try", json={"scenario": "reconcile"})
            assert r.status_code == 200, r.text
            sid, tok = r.json()["session_id"], r.json()["guest_token"]
            for _ in range(100):
                if (await anon.get(f"/public/sessions/{sid}", headers={"X-Guest-Token": tok})).json().get("lifecycle"):
                    break
                await asyncio.sleep(0.05)
        r = await anon.post("/public/try", json={"scenario": "reconcile"})
        assert r.status_code == 429 and "last hour" in r.json()["detail"]


@pytest.mark.asyncio
async def test_guest_mode_switch(app_env, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "guest_mode_enabled", False)
    app, _ = app_env
    async with client(app) as anon:
        assert (await anon.post("/public/try", json={"scenario": "reconcile"})).status_code == 403


@pytest.mark.asyncio
async def test_replay_highlights_come_from_signed_evidence(app_env):
    app, _ = app_env
    async with client(app, headers=AUTH) as op, client(app) as anon:
        sid = (await op.post("/sessions", json={"task": "reconcile"})).json()["session_id"]
        await wait_closed(op, sid)
        await op.post(f"/sessions/{sid}/feature")
        r = next(x for x in (await anon.get("/public/replays")).json() if x["session_id"] == sid)
        texts = [h["text"] for h in r["highlights"]]
        assert r["artifact_verdict"] == "VERIFIED"
        assert texts[0].startswith("Answer independently re-calculated")
        assert "Signed record valid and complete" in texts
        assert not any("stop" in t.lower() for t in texts)          # nothing claimed that didn't happen


@pytest.mark.asyncio
async def test_replay_highlights_never_claim_correctness_for_integrity_only(app_env):
    app, holder = app_env
    from backend.tests.fakes import FakeRuntime
    holder["runtime"] = FakeRuntime([{"stdout": "42\n", "stderr": "", "exit_code": 0}])
    async with client(app, headers=AUTH) as op, client(app) as anon:
        sid = (await op.post("/sessions", json={"task": "print the answer to everything"})).json()["session_id"]
        await wait_closed(op, sid)
        await op.post(f"/sessions/{sid}/feature")
        r = next(x for x in (await anon.get("/public/replays")).json() if x["session_id"] == sid)
        assert r["artifact_verdict"] != "VERIFIED"
        assert not any("re-calculated: correct" in h["text"] for h in r["highlights"])


@pytest.mark.asyncio
async def test_bare_number_output_gets_integrity_only_proof_not_500(app_env):
    """QA bug: the old retry preset printed a row count; the proof endpoint returned 500."""
    app, holder = app_env
    from backend.tests.fakes import FakeRuntime
    holder["runtime"] = FakeRuntime([{"stdout": "20000\n", "stderr": "", "exit_code": 0}])
    async with client(app) as anon:
        r = (await anon.post("/public/try", json={"scenario": "memory"})).json()
        sid, g = r["session_id"], {"X-Guest-Token": r["guest_token"]}
        for _ in range(100):
            if (await anon.get(f"/public/sessions/{sid}", headers=g)).json().get("lifecycle"):
                break
            await asyncio.sleep(0.05)
        resp = await anon.get(f"/public/sessions/{sid}/evidence", headers=g)
        assert resp.status_code == 200, resp.text
        ev = resp.json()
        assert ev["artifact_verification"]["verdict"] == "INTEGRITY_ONLY"
        assert ev["verification"]["artifact_correctness"] == "NOT_APPLICABLE"


@pytest.mark.asyncio
async def test_pause_reports_the_finished_kernel_freeze(app_env):
    """QA bug: Freeze said 'not confirmed by the kernel' while the banner said 'kernel confirms'."""
    app, holder = app_env
    from backend.tests.fakes import FakeRuntime
    holder["runtime"] = FakeRuntime([{"stdout": "", "stderr": "", "exit_code": 0}], delay=30)
    async with client(app) as anon:
        r = (await anon.post("/public/try", json={"scenario": "runaway"})).json()
        sid, g = r["session_id"], {"X-Guest-Token": r["guest_token"]}
        await asyncio.sleep(0.2)
        p = (await anon.post(f"/public/sessions/{sid}/pause", headers=g)).json()
        assert p["paused"] is True and p["mode"] == "KERNEL_FREEZE" and p["verified_frozen"] is True
        ev = (await anon.get(f"/public/sessions/{sid}/events", headers=g)).json()
        pa = [e for e in ev if e["event_type"] == "pause_applied"][-1]["payload"]
        assert pa["verified_frozen"] is True                     # API and signed record agree
        k = (await anon.post(f"/public/sessions/{sid}/kill", headers=g)).json()
        assert k["verdict"] == "KILL_CONFIRMED"


@pytest.mark.asyncio
async def test_remaining_guest_runs_shown_before_the_click(app_env):
    app, _ = app_env
    from backend.config import settings
    async with client(app) as anon:
        before = (await anon.get("/public/scenarios")).json()["guest_remaining"]
        assert before == settings.guest_per_ip_per_hour
        await anon.post("/public/try", json={"scenario": "reconcile"})
        assert (await anon.get("/public/scenarios")).json()["guest_remaining"] == before - 1


def test_retry_preset_cannot_be_solved_offline():
    from backend.scenarios import BY_ID
    t = BY_ID["retry"]["task"]
    assert "https://" in t and "do not read local files" in t and "do not catch exceptions" in t
