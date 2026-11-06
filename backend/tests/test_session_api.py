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
    monkeypatch.setattr(settings, "usage_db_path", str(tmp_path / "usage.db"))
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
        return rt, "microsandbox", {"primary": "microsandbox", "fallback_used": False}

    monkeypatch.setattr(sm.RuntimeSelector, "create", staticmethod(fake_create))
    monkeypatch.setattr(sm, "CgroupController", lambda sid, root=None: holder.get("cgroup") or FakeCgroup())
    monkeypatch.setattr(sm, "proc_starttime", lambda pid: 12345)
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
        assert r.json()["containment"]["passed"] is True, r.json()["containment"]
        sid = r.json()["session_id"]
        s = await wait_closed(c, sid)
        assert s["agent_status"] == "completed"
        lc = s["lifecycle"]
        assert lc["workload_gone"] and lc["cgroup_removed"] and lc["inference_revoked"]

        ev = (await c.get(f"/sessions/{sid}/evidence")).json()
        assert ev["chain_valid"], ev["chain_detail"]
        av = ev["artifact_verification"]
        assert av["level"] == 2 and av["verdict"] == "VERIFIED", av
        v = ev["verification"]
        assert v == {"ledger_integrity": "VALID", "input_integrity": "VALID", "artifact_integrity": "VALID",
                     "artifact_correctness": "VALID", "containment": "VALID", "resource_limits": "VALID",
                     "termination": "NOT_APPLICABLE", "execution_cleanup": "VALID",
                     "inference_accounting": "VALID"}, v

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
        assert av["verdict"] == "FAILED" and av["correctness_detail"]["expected"]["missing_payments"] == 1
        assert (await c.get(f"/sessions/{sid}/evidence")).json()["verification"]["artifact_correctness"] == "FAILED"


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
        assert ev["lifecycle"]["clean"] is True and ev["verification"]["execution_cleanup"] == "VALID"
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


# ---------------------------------------------------------------- start failures & cleanup

async def _start(c, **kw):
    return await c.post("/sessions", json={"task": "reconcile", **kw})


def _auth_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                             headers={"Authorization": "Bearer test-token-123"})


@pytest.mark.asyncio
@pytest.mark.parametrize("override,failed", [
    ({"uid": 0}, "unprivileged_execution"),
    ({"inputs_read_only": False, "writable_inputs": ["orders.csv"]}, "inputs_read_only"),
    ({"egress_blocked": False}, "network_egress_blocked"),
])
async def test_failed_containment_refuses_session_and_cleans_up(app_env, override, failed):
    app, holder = app_env
    rt, cg = FakeRuntime(), FakeCgroup()
    rt.probe_override = override
    holder.update(runtime=rt, cgroup=cg)
    async with _auth_client(app) as c:
        r = await _start(c)
        assert r.status_code == 503
        d = r.json()["detail"]
        assert d["category"] == "containment_check_failed" and failed in d["detail"]
        assert rt.stopped and not cg.populated                     # runtime + cgroup cleaned
        ev = (await c.get(f"/sessions/{d['session_id']}/evidence")).json()
        assert ev["session_started"] is False and ev["verification"]["containment"] == "FAILED"
        assert failed in ev["containment"]["failed_checks"]
        assert ev["start_failure"]["cleanup"]["cgroup_removed"] is True
        # failed start releases its usage reservation
        assert (await _start(c)).status_code == 503                 # still refused, not 429


@pytest.mark.asyncio
async def test_incomplete_cgroup_attachment_refuses_session(app_env):
    app, holder = app_env
    cg = FakeCgroup()
    cg.attach_ok = False
    holder["cgroup"] = cg
    async with _auth_client(app) as c:
        r = await _start(c)
        assert r.status_code == 503 and r.json()["detail"]["category"] == "sandbox_attach_failed"
        assert not cg.populated


@pytest.mark.asyncio
async def test_runtime_start_failure_after_cgroup_creation_destroys_cgroup(app_env, monkeypatch):
    import backend.services.session_manager as sm
    app, holder = app_env
    cg = FakeCgroup()
    holder["cgroup"] = cg

    async def boom(*a, **kw):
        raise RuntimeError("microsandbox unavailable (no /dev/kvm)")
    monkeypatch.setattr(sm.RuntimeSelector, "create", staticmethod(boom))
    async with _auth_client(app) as c:
        r = await _start(c)
        assert r.status_code == 503 and r.json()["detail"]["category"] == "sandbox_runtime_unavailable"
        assert not cg.populated


@pytest.mark.asyncio
async def test_one_failed_cleanup_step_does_not_skip_others(app_env):
    app, holder = app_env
    rt, cg = FakeRuntime(), FakeCgroup()
    rt.probe_override = {"uid": 0}

    async def stop_raises(force=False):
        raise RuntimeError("sdk stop exploded")
    rt.stop = stop_raises
    holder.update(runtime=rt, cgroup=cg)
    async with _auth_client(app) as c:
        d = (await _start(c)).json()["detail"]
        ev = (await c.get(f"/sessions/{d['session_id']}/evidence")).json()
        cl = ev["start_failure"]["cleanup"]
        assert cl["runtime"]["removed"] is False and "exploded" in cl["runtime"]["error"]
        assert cl["cgroup_kill_ok"] is True and cl["cgroup_removed"] is True


@pytest.mark.asyncio
async def test_failed_sandbox_removal_is_reported_not_hidden(app_env):
    app, holder = app_env
    rt = FakeRuntime([{"stdout": '{"missing_payments": 1, "amount_mismatches": 1}\n', "stderr": "", "exit_code": 0}])
    holder["runtime"] = rt
    async with _auth_client(app) as c:
        sid = (await _start(c)).json()["session_id"]
        rt.stop_fails = True
        s = await wait_closed(c, sid)
        assert s["lifecycle"]["sandbox_removed"] is False and s["lifecycle"]["clean"] is False
        ev = (await c.get(f"/sessions/{sid}/evidence")).json()
        assert ev["verification"]["execution_cleanup"] == "FAILED"


@pytest.mark.asyncio
async def test_dollar_cap_requires_verified_price(app_env):
    app, _ = app_env
    async with _auth_client(app) as c:
        r = await _start(c, cost_limit_usd=0.5)
        assert r.status_code == 422 and "verified pricing" in r.text


# ---------------------------------------------------------------- evidence negatives

async def _completed(app):
    c = _auth_client(app)
    sid = (await _start(c)).json()["session_id"]
    await wait_closed(c, sid)
    return c, sid


@pytest.mark.asyncio
async def test_modified_input_snapshot_fails_input_and_correctness(app_env):
    from backend.config import settings
    from pathlib import Path
    app, _ = app_env
    c, sid = await _completed(app)
    f = Path(settings.ledger_dir) / sid / "inputs" / "orders.csv"
    f.chmod(0o644)
    f.write_text(f.read_text() + "E,50.00\n")
    v = (await c.get(f"/sessions/{sid}/evidence")).json()["verification"]
    assert v["input_integrity"] == "FAILED" and v["artifact_correctness"] == "FAILED"
    await c.aclose()


@pytest.mark.asyncio
async def test_modified_artifact_fails_integrity(app_env):
    from backend.config import settings
    from pathlib import Path
    app, _ = app_env
    c, sid = await _completed(app)
    (Path(settings.ledger_dir) / sid / "artifacts" / f"{sid}_final_stdout.txt").write_text('{"missing_payments": 1, "amount_mismatches": 1} ')
    v = (await c.get(f"/sessions/{sid}/evidence")).json()["verification"]
    assert v["artifact_integrity"] == "FAILED"
    await c.aclose()


@pytest.mark.asyncio
async def test_path_traversal_in_ledger_is_refused(app_env):
    from backend.api.evidence import _safe_path
    assert _safe_path("abc123", "../../etc/passwd") is None
    assert _safe_path("abc123", "inputs/orders.csv") is not None


def test_limits_mismatch_and_missing_revocation_fail():
    from backend.api.evidence import _verify_cleanup, _verify_limits
    entries = [
        {"event_type": "session_created", "payload": {"memory_plan": {"host_memory_max_bytes": 536870912}}},
        {"event_type": "cgroup_configured", "payload": {
            "readback": {"cpu.max": "100000 100000", "memory.max": "max", "pids.max": "512"},
            "expected": {"cpu.max": "100000 100000", "memory.max": "536870912", "pids.max": "512"},
            "capabilities": {"cgroup.kill": True, "cpu.stat": True, "memory.current": True, "cgroup.events": True}}},
        {"event_type": "session_closed", "payload": {"agent_terminated": True, "inference_revoked": False,
                                                    "sandbox_removed": True, "workload_gone": True,
                                                    "cgroup_removed": True}},
    ]
    lim = _verify_limits(entries)
    assert lim["verdict"] == "FAILED" and lim["checks"]["memory.max_equals_memory_budget"] is False
    assert _verify_cleanup(entries)["verdict"] == "FAILED"


@pytest.mark.asyncio
async def test_kill_and_pause_race_ends_in_kill_and_resume_is_refused(app_env):
    app, holder = app_env
    holder["runtime"] = FakeRuntime([{"stdout": "", "stderr": "", "exit_code": 0}], delay=10)
    async with _auth_client(app) as c:
        sid = (await _start(c)).json()["session_id"]
        await asyncio.sleep(0.1)
        p, k = await asyncio.gather(c.post(f"/sessions/{sid}/pause"), c.post(f"/sessions/{sid}/kill"))
        assert k.json()["verdict"] == "KILL_CONFIRMED"
        s = await wait_closed(c, sid)
        assert s["state"] == "KILL"


@pytest.mark.asyncio
async def test_closed_session_keeps_final_measurements_and_wall_clock(app_env):
    app, holder = app_env
    cg = FakeCgroup()
    cg.cpu, cg.peak = 2_500_000, 200 * 2**20
    holder["cgroup"] = cg
    async with _auth_client(app) as c:
        sid = (await _start(c)).json()["session_id"]
        s1 = await wait_closed(c, sid)
        await asyncio.sleep(0.5)
        s2 = (await c.get(f"/sessions/{sid}")).json()
        assert s2["cpu_usec"] == 2_500_000 and s2["memory_peak_bytes"] == 200 * 2**20
        assert s2["wall_seconds"] == s1["wall_seconds"]       # frozen at close
