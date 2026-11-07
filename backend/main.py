import logging
import os
import shutil
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api import evidence, governance, sessions, stream
from backend import security
from backend.config import settings
from backend.database import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agentlimit")

# No interactive API docs on a public deployment (they sit behind auth anyway).
_docs = {} if not settings.public_deployment else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="AgentLimit", version="1.5.0", debug=False,
              description="External runtime governor for autonomous AI agents on Vultr.", **_docs)

app.add_middleware(security.AuthMiddleware)
_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
if _origins:  # default: same-origin only (dashboard is served by this app)
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=True,
                       allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])

app.include_router(security.router)
app.include_router(sessions.router)
app.include_router(stream.router)
app.include_router(governance.router)
app.include_router(evidence.router)

_inference_status: dict = {"checked": False}


def _infra_status() -> dict:
    try:
        import microsandbox  # noqa: F401
        sdk = getattr(microsandbox, "__version__", None) or "installed"
    except Exception:
        sdk = None
    controllers = ""
    try:
        controllers = Path("/sys/fs/cgroup/cgroup.controllers").read_text().strip()
    except Exception:
        pass
    return {
        "kvm": Path("/dev/kvm").exists(),
        "cgroup_v2": bool(controllers),
        "cgroup_controllers": controllers,
        "microsandbox_sdk": sdk,
        "docker_fallback_enabled": settings.allow_docker_fallback,
        "docker_cli": bool(shutil.which("docker")),
        "running_as_root": os.geteuid() == 0 if hasattr(os, "geteuid") else None,
    }


@app.on_event("startup")
async def _startup():
    await init_db()
    from backend.services.session_manager import session_manager
    log.info("infra: %s", _infra_status())
    log.info("evidence signing key fingerprint: %s", session_manager.signer.fingerprint())
    if not settings.access_token and not settings.auth_disabled:
        log.error("ACCESS_TOKEN is not set: all session APIs will refuse requests (fail-closed)")
    try:
        await session_manager.recover()
    except Exception as e:
        log.error("startup recovery failed: %s", e)
    session_manager.start_watchdog()
    if not settings.vultr_inference_key:
        _inference_status.update(checked=True, ok=False, error="VULTR_INFERENCE_KEY not set")
        log.error("VULTR_INFERENCE_KEY not set")
        return
    try:
        from backend.orchestrator.inference_adapter import list_models
        models = await list_models()
        from backend.orchestrator import model_registry
        reg = model_registry.validate(models)
        log.info("model validation: default_available=%s downgrade=%s (%s)",
                 reg["default_available"], reg["downgrade_enabled"], reg["downgrade_reason"])
        missing = [m for m in {settings.vultr_model_default, settings.vultr_model_cheap} if m and m not in models]
        _inference_status.update(checked=True, ok=not missing, available_models=models,
                                 configured={"default": settings.vultr_model_default,
                                             "cheap": settings.vultr_model_cheap},
                                 missing=missing)
        if missing:
            log.error("configured model IDs not offered by Vultr: %s. Available: %s", missing, models)
    except Exception as e:
        _inference_status.update(checked=True, ok=False, error=str(e)[:300])
        log.error("inference check failed: %s", e)


@app.get("/health")
async def health(request: Request):
    infra = _infra_status()
    body = {"status": "ok", "alive": True, "ready_for_governed_sessions": _readiness()["ready"],
            "auth_configured": bool(settings.access_token) or settings.auth_disabled}
    if security.is_authenticated(request):   # details only for authenticated callers
        body.update(infra=infra, inference=_inference_status)
    return body


def _readiness() -> dict:
    """Can this server SAFELY create a governed session right now?"""
    import os
    import tempfile
    from backend.orchestrator import model_registry
    from backend.orchestrator.inference_pricing import has_price
    from backend.services.session_manager import session_manager
    infra = _infra_status()
    ctrl = set(infra["cgroup_controllers"].split())
    checks = {}
    checks["kvm_accessible"] = os.access("/dev/kvm", os.R_OK | os.W_OK)
    checks["cgroup_v2"] = infra["cgroup_v2"]
    checks["cgroup_controllers_cpu_memory_pids"] = {"cpu", "memory", "pids"} <= ctrl
    checks["running_as_root"] = bool(infra["running_as_root"])
    checks["microsandbox_sdk"] = bool(infra["microsandbox_sdk"])
    checks["docker_fallback_disabled"] = not settings.allow_docker_fallback
    try:
        sig = session_manager.signer.sign(b"ready-probe")
        checks["signing_key_works"] = session_manager.signer.verify(
            session_manager.signer.public_key_b64(), b"ready-probe", sig)
    except Exception:
        checks["signing_key_works"] = False
    try:
        with tempfile.NamedTemporaryFile(dir=settings.ledger_dir, delete=True):
            pass
        checks["ledger_dir_writable"] = True
    except Exception:
        checks["ledger_dir_writable"] = False
    checks["inference_key_configured"] = bool(settings.vultr_inference_key)
    checks["default_model_available"] = bool(model_registry.STATE.get("default_available"))
    checks["access_token_configured"] = bool(settings.access_token) and len(settings.access_token) >= 24
    checks["auth_enabled"] = not settings.auth_disabled or not settings.public_deployment
    wd = session_manager.watchdog.status()
    checks["watchdog_running"] = wd["running"] and not session_manager.watchdog.stalled()
    info = {
        "default_model_priced": has_price(settings.vultr_model_default),
        "dollar_caps_available": has_price(settings.vultr_model_default),
        "model_downgrade": {"enabled": model_registry.STATE.get("downgrade_enabled", False),
                            "reason": model_registry.STATE.get("downgrade_reason")},
        "watchdog": wd,
        "infrastructure_ready": all(v for k, v in checks.items()
                                    if k not in ("inference_key_configured", "default_model_available")),
        "model_ready": checks["inference_key_configured"] and checks["default_model_available"],
        "cgroup_freeze_available": Path("/sys/fs/cgroup/cgroup.freeze").exists() or "cgroup.freeze" in ctrl or None,
    }
    return {"ready": all(checks.values()), "checks": checks,
            "failed": [k for k, v in checks.items() if not v], "info": info}


@app.get("/ready")
async def ready(request: Request):
    r = _readiness()
    if security.is_authenticated(request):
        try:
            from backend.services.session_manager import session_manager
            r["usage"] = session_manager.usage.summary()
        except Exception:
            pass
        return r
    return {"ready": r["ready"]}


# Serve the built dashboard from the same origin when present (single-port deploy).
_dist = Path(settings.frontend_dist)
if _dist.is_dir() and (_dist / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def _index():
        return FileResponse(_dist / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=False)
