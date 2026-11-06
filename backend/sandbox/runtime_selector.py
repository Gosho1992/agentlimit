import logging
from pathlib import Path

from backend.config import settings
from backend.sandbox.docker_fallback import DockerFallbackRuntime
from backend.sandbox.microsandbox_runtime import MicrosandboxRuntime

log = logging.getLogger("agentlimit.runtime.selector")


class RuntimeSelector:
    """Microsandbox (KVM microVM) is the supported, demonstrated runtime.

    Docker is an explicit opt-in for machines without /dev/kvm
    (ALLOW_DOCKER_FALLBACK=true) and does NOT carry the microVM isolation
    claims. Before any fallback, a partially created Microsandbox resource is
    stopped and removed, and that removal must be confirmed.

    Returns (runtime, runtime_type, selection_report).
    """

    @staticmethod
    async def create(session_id: str, workspace_path: Path | None = None, memory_mb: int | None = None):
        rt = MicrosandboxRuntime(session_id, memory_mb=memory_mb)
        try:
            await rt.start(workspace_path)
            return rt, "microsandbox", {"primary": "microsandbox", "fallback_used": False}
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"[:300]
            cleanup = await rt.stop(force=True)
            if not settings.allow_docker_fallback:
                raise RuntimeError(f"microsandbox unavailable ({reason}); docker fallback disabled; "
                                   f"partial-resource cleanup={cleanup}") from e
            if not cleanup.get("removed"):
                raise RuntimeError(f"microsandbox failed ({reason}) and its partial resource could not be "
                                   f"removed ({cleanup}); refusing to start a fallback") from e
            log.warning("[%s] microsandbox failed (%s); ALLOW_DOCKER_FALLBACK=true -> docker", session_id, reason)
            d = DockerFallbackRuntime(session_id, memory_mb=memory_mb)
            await d.start(workspace_path)
            return d, "docker_fallback", {"primary": "microsandbox", "fallback_used": True,
                                          "fallback_reason": reason, "primary_cleanup": cleanup}
