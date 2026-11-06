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

app = FastAPI(title="AgentLimit", version="1.4.0",
              description="External runtime governor for autonomous AI agents on Vultr.")

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
    if not settings.vultr_inference_key:
        _inference_status.update(checked=True, ok=False, error="VULTR_INFERENCE_KEY not set")
        log.error("VULTR_INFERENCE_KEY not set")
        return
    try:
        from backend.orchestrator.inference_adapter import list_models
        models = await list_models()
        missing = [m for m in {settings.vultr_model_default, settings.vultr_model_cheap} if m not in models]
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
    ready = infra["kvm"] and infra["cgroup_v2"] and bool(infra["microsandbox_sdk"]) and _inference_status.get("ok", False)
    body = {"status": "ok", "ready_for_governed_sessions": ready,
            "auth_configured": bool(settings.access_token) or settings.auth_disabled}
    if security.is_authenticated(request):   # details only for authenticated callers
        body.update(infra=infra, inference=_inference_status)
    return body


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
