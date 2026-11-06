import threading

import httpx
import pytest

from backend.services.usage_store import LimitExceeded, UsageStore


@pytest.fixture
def app(monkeypatch, tmp_path):
    from backend.config import settings
    from backend import security
    monkeypatch.setattr(settings, "access_token", "x" * 32)
    monkeypatch.setattr(settings, "auth_disabled", False)
    security._attempts.clear()
    from backend.main import app
    return app


@pytest.mark.asyncio
async def test_spoofed_forwarded_headers_do_not_reset_rate_limit(app):
    t = httpx.ASGITransport(app=app, client=("203.0.113.9", 5555))     # direct internet client
    async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
        codes = []
        for i in range(12):
            r = await c.post("/auth/login", json={"token": "bad"},
                             headers={"X-Forwarded-For": f"10.0.0.{i}", "X-Real-IP": f"10.1.1.{i}"})
            codes.append(r.status_code)
        assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]


@pytest.mark.asyncio
async def test_real_ip_from_local_proxy_is_honoured(app):
    t = httpx.ASGITransport(app=app, client=("127.0.0.1", 5555))       # nginx on localhost
    async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
        for _ in range(10):
            await c.post("/auth/login", json={"token": "bad"}, headers={"X-Real-IP": "198.51.100.1"})
        assert (await c.post("/auth/login", json={"token": "bad"}, headers={"X-Real-IP": "198.51.100.1"})).status_code == 429
        assert (await c.post("/auth/login", json={"token": "bad"}, headers={"X-Real-IP": "198.51.100.2"})).status_code == 401


@pytest.mark.asyncio
async def test_cookie_login_logout_and_protected_evidence(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/sessions/abc/evidence")).status_code == 401
        assert (await c.post("/auth/login", json={"token": "x" * 32})).status_code == 200
        assert (await c.get("/sessions/abc/evidence")).status_code == 404      # authenticated, unknown id
        await c.post("/auth/logout")
        c.cookies.clear()
        assert (await c.get("/sessions/abc/evidence")).status_code == 401
        c.cookies.set("al_session", "forged")
        assert (await c.get("/sessions/abc/evidence")).status_code == 401


@pytest.mark.asyncio
async def test_secure_cookie_flag(app, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "cookie_secure", True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://t") as c:
        r = await c.post("/auth/login", json={"token": "x" * 32})
        sc = r.headers["set-cookie"].lower()
        assert "secure" in sc and "httponly" in sc and "samesite=strict" in sc


@pytest.mark.asyncio
async def test_ready_hides_details_from_anonymous(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert set((await c.get("/ready")).json()) == {"ready"}
        r = (await c.get("/ready", headers={"Authorization": "Bearer " + "x" * 32})).json()
        assert "checks" in r and r["ready"] is False         # no KVM here -> never falsely ready


def test_usage_limits_survive_restart(tmp_path, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "max_sessions_per_hour", 2)
    monkeypatch.setattr(settings, "max_tokens_per_day", 100_000)
    db = str(tmp_path / "u.db")
    s1 = UsageStore(db)
    s1.reserve("a", 30_000)
    s1.close("a", "closed", actual_tokens=20_000)
    s1.reserve("b", 30_000)
    s2 = UsageStore(db)                         # "restart"
    assert s2.mark_interrupted() == ["b"]
    summ = s2.summary()
    assert summ["sessions_last_hour"] == 2 and summ["tokens_committed_24h"] == 50_000
    with pytest.raises(LimitExceeded, match="hourly"):
        s2.reserve("c", 1000)


def test_reservation_is_atomic_under_concurrency(tmp_path, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "max_concurrent_sessions", 2)
    monkeypatch.setattr(settings, "max_sessions_per_hour", 100)
    store = UsageStore(str(tmp_path / "u.db"))
    ok, errs = [], []

    def go(i):
        try:
            store.reserve(f"s{i}", 1000)
            ok.append(i)
        except LimitExceeded as e:
            errs.append(e)
    threads = [threading.Thread(target=go, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 2 and len(errs) == 10
