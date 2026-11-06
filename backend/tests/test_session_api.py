"""Full HTTP-level session lifecycle with fake kernel/VM, real everything else."""
import asyncio
import json

import httpx
import pytest

from backend.evidence.verifier import DeterministicVerifier
from backend.tests.fakes import FakeCgroup, FakeRuntime, mock_inference_transport


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ledger_dir", str(tmp_path / "ledger"))
    monkeypatch.setattr(settings, "signer_key_path", str(tmp_path / "keys" / "k"))
    monkeypatch.setattr(settings, "poll_interval_ms", 20)
    monkeypatch.setattr(settings, "access_token", "test-token-123")
    monkeypatch.setattr(settings, "auth_disabled", False)
    monkeypatch.setattr(settings, "inflight_drain_s", 1.0)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "orders.csv").write_text("order_id,amount\nA,10.00\nB,20.00\nC,30.00\nD,40.00\n")
    (ws / "payments.csv").write_text("order_id,paid_amount\nA,10.00\nB,20.00\nC,99.00\n")
    monkeypatch.setenv("AGENTLIMIT_WORKSPACE", str(ws))

    import backend.services.session_manager as sm
    from backend.orchestrator import inference_adapter as ia
    holder = {}

    async def fake_create(session_id, workspace_path=None, memory_mb=None):
        rt = holder.get("runtime") or FakeRuntime([{"stdout": '{"missing_payments": 1, "amount_mismatches": 1}\n',
                                                    "stderr": "", "exit_code": 0}])
        return rt, "microsandbox"

    monkeypatch.setattr(sm.RuntimeSelector, "create", staticmethod(fake_create))
    monkeypatch.setattr(sm, "CgroupController", lambda sid, root=None: FakeCgroup())
    monkeypatch.setattr(sm, "session_manager", sm.SessionManager())
    import backend.api.sessions as a1, backend.api.governance as a2, backend.api.evidence as a3, backend.api.stream as a4
    for mod in (a1, a2, a3, a4):
        monkeypatch.setattr(mod, "session_manager", sm.session_manager)
    orig_init = ia.InferenceAdapter.__init__

    def init(self, *a, **kw):
        kw.setdefault("transport", holder.get("transport") or mock_inference_transport(
            code="print('{\"missing_payments\": 1, \"amount_mismatches\": 1}')"))
        orig_init(self, *a, **kw)
    monkeypatch.setattr(ia.InferenceAdapter, "__init__", init)
    from backend.main import app
    return app, holder


async def wait_closed(c, sid, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        s = (await c.get(f"/sessions/{sid}")).json()
        if s["lifecycle"]:
            return s
        await asyncio.sleep(0.05)
    raise AssertionError("session did not close")


@pytest.mark.asyncio
async def test_completed_session_is_verified_and_cleaned_up(app_env):
    app, _ = app_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": "Bearer test-token-123"}) as c:
        r = await c.post("/sessions", json={"task": "reconcile"})
        assert r.status_code == 200, r.text
        assert r.json()["containment"]["passed"] is True
        sid = r.json()["session_id"]
        s = await wait_closed(c, sid)
        assert s["agent_status"] == "completed"
        lc = s["lifecycle"]
        assert lc["workload_gone"] and lc["cgroup_removed"] and lc["inference_revoked"]

        ev = (await c.get(f"/sessions/{sid}/evidence")).json()
        assert ev["chain_valid"], ev["chain_detail"]
        av = ev["artifact_verification"]
        assert av["level"] == 2 and av["verdict"] == "VERIFIED", av
        assert av["inputs"]["verified"]
        assert all(v["ok"] for v in ev["containment"].values()), ev["containment"]

        t = (await c.post(f"/sessions/{sid}/evidence/tamper-test")).json()
        assert t["tamper_detected"] and t["original"]["valid"] and not t["tampered_copy"]["valid"]

        exp = await c.get(f"/sessions/{sid}/evidence/export")
        assert exp.headers["X-AgentLimit-Public-Key"] == ev["public_key_b64"]
        assert len(exp.text.strip().splitlines()) == ev["entry_count"]


@pytest.mark.asyncio
async def test_wrong_agent_answer_is_caught_by_verifier(app_env):
    app, holder = app_env
    holder["runtime"] = FakeRuntime([{"stdout": '{"missing_payments": 0, "amount_mismatches": 0}\n',
                                      "stderr": "", "exit_code": 0}])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": "Bearer test-token-123"}) as c:
        sid = (await c.post("/sessions", json={"task": "reconcile"})).json()["session_id"]
        await wait_closed(c, sid)
        av = (await c.get(f"/sessions/{sid}/evidence")).json()["artifact_verification"]
        assert av["verdict"] == "FAILED" and av["correctness"]["expected"]["missing_payments"] == 1


@pytest.mark.asyncio
async def test_operator_kill_reports_verified_verdict(app_env):
    app, holder = app_env
    holder["runtime"] = FakeRuntime([{"stdout": "", "stderr": "", "exit_code": 0}], delay=10)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": "Bearer test-token-123"}) as c:
        sid = (await c.post("/sessions", json={"task": "spin"})).json()["session_id"]
        await asyncio.sleep(0.2)
        k = (await c.post(f"/sessions/{sid}/kill")).json()
        assert k["killed"] is True and k["verdict"] == "KILL_CONFIRMED", k
        assert all(k["checks"].values())
        s = await wait_closed(c, sid)
        assert s["state"] == "KILL" and s["lifecycle"]["kill_verdict"] == "KILL_CONFIRMED"


@pytest.mark.asyncio
async def test_invalid_thresholds_rejected(app_env):
    app, _ = app_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": "Bearer test-token-123"}) as c:
        r = await c.post("/sessions", json={"task": "x", "alert_pct": 90, "recommend_pct": 80})
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_api_requires_auth_and_cookie_login_works(app_env):
    app, _ = app_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/sessions", json={"task": "x"})).status_code == 401
        assert (await c.get("/sessions")).status_code == 401
        h = (await c.get("/health")).json()
        assert "infra" not in h and "inference" not in h          # no detail leak
        assert (await c.post("/auth/login", json={"token": "wrong"})).status_code == 401
        r = await c.post("/auth/login", json={"token": "test-token-123"})
        assert r.status_code == 200 and "al_session" in r.cookies
        assert (await c.get("/sessions")).status_code == 200      # cookie now authenticates
        assert "infra" in (await c.get("/health")).json()


@pytest.mark.asyncio
async def test_fail_closed_without_configured_token(app_env, monkeypatch):
    from backend.config import settings
    app, _ = app_env
    monkeypatch.setattr(settings, "access_token", "")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/sessions")).status_code == 503


@pytest.mark.asyncio
async def test_hourly_session_cap(app_env, monkeypatch):
    from backend.config import settings
    app, _ = app_env
    monkeypatch.setattr(settings, "max_sessions_per_hour", 1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": "Bearer test-token-123"}) as c:
        sid = (await c.post("/sessions", json={"task": "reconcile"})).json()["session_id"]
        await wait_closed(c, sid)
        assert (await c.post("/sessions", json={"task": "again"})).status_code == 429


@pytest.mark.asyncio
async def test_archived_session_evidence_after_restart(app_env, monkeypatch):
    import backend.services.session_manager as sm
    app, _ = app_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": "Bearer test-token-123"}) as c:
        sid = (await c.post("/sessions", json={"task": "reconcile"})).json()["session_id"]
        await wait_closed(c, sid)
        sm.session_manager._sessions.clear()                      # simulate a process restart
        ev = (await c.get(f"/sessions/{sid}/evidence")).json()
        assert ev["archived"] and ev["chain_valid"]
        assert ev["artifact_verification"]["verdict"] == "VERIFIED"
        assert ev["lifecycle"]["clean"] is True
        snap = (await c.get(f"/sessions/{sid}")).json()
        assert snap["archived"] and snap["state"] == ev["final_state"]
        assert any(s["session_id"] == sid for s in (await c.get("/sessions")).json())
        assert (await c.post(f"/sessions/{sid}/kill")).status_code == 409


@pytest.mark.asyncio
async def test_recovery_marks_interrupted_sessions(app_env, tmp_path):
    from backend.config import settings
    import backend.services.session_manager as sm
    from backend.evidence.ledger import EvidenceLedger
    led = EvidenceLedger("abc123", sm.session_manager.signer, settings.ledger_dir)
    led.append("session_created", {"task": "t", "budget": {}})
    r = await sm.session_manager.recover()
    assert "abc123" in r["interrupted_sessions"]
    assert led.read_all()[-1]["event_type"] == "session_interrupted"
    assert led.verify_chain(sm.session_manager.signer.public_key_b64())[0]
