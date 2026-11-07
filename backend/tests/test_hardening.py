"""Hardening tests (v1.5): out-of-loop watchdog, ledger completeness
(truncation / deletion / omission), explicit intervention verification,
recommendation-delivery failures, concurrent kill/resume, and an inventory of
every registered API route against the public/private policy."""
import asyncio
import re

import httpx
import pytest

from backend.evidence.ledger import EvidenceLedger, ledger_completeness, verify_entries
from backend.evidence.signer import Signer
from backend.governance.thresholds import State
from backend.governance.watchdog import Watchdog
from backend.tests.fakes import FakeCgroup
from backend.tests.test_engine import make


# ------------------------------------------------------------------ ledger completeness

def _full_ledger(tmp_path, n_middle=5):
    s = Signer(str(tmp_path / "k"))
    led = EvidenceLedger("c", s, str(tmp_path / "l"))
    led.append("session_created", {"task": "t"})
    for i in range(n_middle):
        led.append("resource_sample", {"cpu_usage_usec": i})
    led.append("kill_verified", {"verdict": "KILL_CONFIRMED"})
    led.append("session_closed", {"clean": True})
    return s, led


def test_complete_ledger_is_complete(tmp_path):
    s, led = _full_ledger(tmp_path)
    e = led.read_all()
    assert verify_entries(e, s.public_key_b64())[0]
    assert ledger_completeness(e)[0] == "COMPLETE"


def test_truncated_tail_keeps_integrity_but_is_never_complete(tmp_path):
    s, led = _full_ledger(tmp_path)
    e = led.read_all()
    for cut in range(1, len(e)):
        prefix = e[:cut]
        assert verify_entries(prefix, s.public_key_b64())[0]          # a prefix is still a valid chain…
        assert ledger_completeness(prefix)[0] == "INCOMPLETE"          # …so completeness must catch it


def test_deleted_middle_entry_breaks_chain(tmp_path):
    s, led = _full_ledger(tmp_path)
    e = led.read_all()
    del e[3]
    ok, detail = verify_entries(e, s.public_key_b64())
    assert not ok and "broken link" in detail


def test_removed_head_breaks_chain(tmp_path):
    s, led = _full_ledger(tmp_path)
    e = led.read_all()[1:]
    assert not verify_entries(e, s.public_key_b64())[0]
    assert ledger_completeness(e)[0] == "INCOMPLETE"


def test_forged_terminal_event_without_key_fails(tmp_path):
    s, led = _full_ledger(tmp_path)
    e = led.read_all()[:3]
    forged = dict(e[-1], event_type="session_closed", prev_hash=e[-1]["entry_hash"], payload={})
    e.append(forged)
    assert not verify_entries(e, s.public_key_b64())[0]


def test_live_and_interrupted_states(tmp_path):
    s = Signer(str(tmp_path / "k"))
    led = EvidenceLedger("c", s, str(tmp_path / "l"))
    led.append("session_created", {})
    led.append("resource_sample", {})
    assert ledger_completeness(led.read_all(), live=True)[0] == "IN_PROGRESS"
    led.append("session_interrupted", {"reason": "restart"})
    assert ledger_completeness(led.read_all())[0] == "INTERRUPTED"


def test_offline_cli_reports_incomplete_with_exit_3(tmp_path, capsys):
    from backend.scripts.verify_ledger import main
    s, led = _full_ledger(tmp_path)
    assert main(str(led.path), s.public_key_b64()) == 0
    lines = led.path.read_text().splitlines()
    led.path.write_text("\n".join(lines[:-2]) + "\n")                 # hide the kill + close
    assert main(str(led.path), s.public_key_b64()) == 3
    assert "INCOMPLETE" in capsys.readouterr().out


def test_ledger_append_is_thread_safe(tmp_path):
    import threading
    s = Signer(str(tmp_path / "k"))
    led = EvidenceLedger("c", s, str(tmp_path / "l"))
    led.append("session_created", {})
    ts = [threading.Thread(target=lambda: [led.append("x", {"i": i}) for i in range(50)]) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert verify_entries(led.read_all(), s.public_key_b64())[0]


# ------------------------------------------------------------------ watchdog

class _RT:
    def __init__(self, sid, engine, ledger, cg):
        self.session_id, self.engine, self.ledger, self.cgroup, self.closed = sid, engine, ledger, cg, False


def test_watchdog_quiet_while_heartbeat_fresh(tmp_path):
    eng, orch, cg, ledger, *_ = make(tmp_path)
    now = [100.0]
    wd = Watchdog(lambda: [_RT("s", eng, ledger, cg)], stall_s=15, clock=lambda: now[0])
    wd.beat()
    now[0] += 10
    assert wd.check() == [] and cg.populated


@pytest.mark.asyncio
async def test_watchdog_kills_on_stall_then_engine_completes_verified_kill(tmp_path):
    eng, orch, cg, ledger, *_ = make(tmp_path)
    now = [100.0]
    rt = _RT("s", eng, ledger, cg)
    wd = Watchdog(lambda: [rt], stall_s=15, clock=lambda: now[0])
    wd.beat()
    now[0] += 20                                               # loop "stalled" for 20 s
    assert wd.check() == ["s"]
    assert not cg.populated                                    # workload killed from the thread
    assert wd.check() == []                                    # fires once per session
    ev = [e for e in ledger.read_all() if e["event_type"] == "watchdog_kill"][-1]["payload"]
    assert ev["cgroup_kill_ok"] is True and ev["heartbeat_age_s"] >= 15
    agent = asyncio.create_task(asyncio.sleep(30))
    orch.attach_task(agent)
    await eng._tick()                                          # loop recovers -> verified KILL
    assert eng.state == State.KILL
    types = [e["event_type"] for e in ledger.read_all()]
    assert "kill_verified" in types or "kill_failed" in types
    assert verify_entries(ledger.read_all(), Signer(str(tmp_path / "key")).public_key_b64())[0]


def test_watchdog_skips_closed_sessions(tmp_path):
    eng, orch, cg, ledger, *_ = make(tmp_path)
    rt = _RT("s", eng, ledger, cg)
    rt.closed = True
    now = [0.0]
    wd = Watchdog(lambda: [rt], stall_s=1, clock=lambda: now[0])
    now[0] = 5
    assert wd.check() == [] and cg.populated


@pytest.mark.asyncio
async def test_watchdog_thread_detects_real_blocked_loop(tmp_path):
    import time
    eng, orch, cg, ledger, *_ = make(tmp_path)
    rt = _RT("s", eng, ledger, cg)
    wd = Watchdog(lambda: [rt], stall_s=0.3, interval_s=0.05)
    hb = asyncio.create_task(wd.heartbeat_task())
    wd.start()
    await asyncio.sleep(0.2)
    assert cg.populated
    time.sleep(0.8)                      # block the event loop (simulated stall)
    assert not cg.populated and "s" in wd.fired
    wd.stop()
    hb.cancel()


# ------------------------------------------------------------------ interventions report verification

@pytest.mark.asyncio
async def test_failed_throttle_is_not_reported_as_throttled(tmp_path):
    eng, orch, cg, ledger, *_ = make(tmp_path)
    cg.set_cpu_quota = lambda q, period=100_000: None             # kernel ignores the write
    await eng.interventions.throttle()
    p = [e for e in ledger.read_all() if e["event_type"] == "throttle_applied"][-1]["payload"]
    assert p["requested"] is True and p["verified"] is False
    assert eng.interventions.throttled is False


@pytest.mark.asyncio
async def test_memory_high_recommendation_verified_by_readback(tmp_path):
    eng, orch, cg, ledger, *_ = make(tmp_path)
    assert await eng.interventions.apply_recommendation({"action": "set_memory_high", "rule_id": "memory_pressure"})
    p = [e for e in ledger.read_all() if e["event_type"] == "recommendation_applied"][-1]["payload"]
    assert p["verified"] is True and p["memory.high_readback"] == str(cg.memory_high)


@pytest.mark.asyncio
async def test_recommendation_delivery_failure_is_recorded(tmp_path):
    eng, orch, cg, ledger, *_ = make(tmp_path)

    async def boom(_):
        raise RuntimeError("agent mailbox closed")
    orch.receive_recommendation = boom
    real = eng.recommender.evaluate
    eng.recommender.evaluate = lambda snap: real(dict(snap, consecutive_identical_errors=5))
    await eng._evaluate_recommendations("test")
    types = [e["event_type"] for e in ledger.read_all()]
    assert "recommendation_issued" in types
    assert "recommendation_delivery_failed" in types


# ------------------------------------------------------------------ concurrent control actions

@pytest.mark.asyncio
async def test_concurrent_auto_kill_manual_kill_and_resume_yield_one_verified_kill(tmp_path):
    eng, orch, cg, ledger, budget, _ = make(tmp_path)
    agent = asyncio.create_task(asyncio.sleep(30))
    orch.attach_task(agent)
    cg.cpu = budget.cpu_budget_usec + 1                         # hard limit -> automatic KILL path
    await asyncio.gather(eng._tick(), eng.kill_now("operator_kill"), eng.resume(),
                         eng.kill_now("operator_kill_2"), return_exceptions=True)
    assert eng.state == State.KILL
    types = [e["event_type"] for e in ledger.read_all()]
    assert types.count("kill_verified") + types.count("kill_failed") == 1
    assert "resume_applied" not in types[types.index("kill_requested"):] if "kill_requested" in types else True
    r = await eng.resume()                                     # KILL is irreversible
    assert eng.state == State.KILL and (r or {}).get("ok") is not True


# ------------------------------------------------------------------ route inventory

PUBLIC_ROUTES = {"/", "/health", "/ready", "/auth/login", "/auth/logout", "/auth/status"}


@pytest.fixture
def app(monkeypatch):
    from backend.config import settings
    from backend import security
    monkeypatch.setattr(settings, "access_token", "x" * 32)
    monkeypatch.setattr(settings, "auth_disabled", False)
    security._attempts.clear()
    from backend.main import app
    return app


@pytest.mark.asyncio
async def test_every_non_public_route_requires_auth(app):
    from fastapi.routing import APIRoute

    def walk(routes, prefix=""):
        for r in routes:
            if isinstance(r, APIRoute):
                yield prefix, r
            elif hasattr(r, "original_router"):            # FastAPI >= 0.13x include wrapper
                yield from walk(r.original_router.routes, prefix + (r.original_router.prefix or ""))
    routes = []
    for prefix, r in walk(app.routes):
        r.full_path = r.path if r.path.startswith(prefix) else prefix + r.path
        routes.append(r)
    assert len(routes) > 10
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for r in routes:
            path = re.sub(r"\{[^}]+\}", "probe", r.full_path)
            for m in r.methods - {"HEAD"}:
                resp = await c.request(m, path)
                if r.full_path in PUBLIC_ROUTES:
                    continue
                assert resp.status_code == 401, f"{m} {r.full_path} reachable without auth ({resp.status_code})"


@pytest.mark.asyncio
async def test_non_normalised_paths_cannot_ride_public_prefixes(app):
    from backend.security import is_public_path
    for p in ("/auth/../sessions", "/assets/../sessions", "//sessions", "/auth/x", "/auth/login/../../sessions",
              "/readyx", "/health/../sessions", "/assets/..\\sessions"):
        assert not is_public_path(p) or p.startswith("/assets/") and ".." not in p, p
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/auth/anything-new")).status_code == 401
        assert (await c.get("/docs")).status_code in (401, 404)
        assert (await c.get("/openapi.json")).status_code in (401, 404)
