import logging
from pathlib import Path

from backend.config import settings
from backend.sandbox.docker_fallback import DockerFallbackRuntime
from backend.sandbox.microsandbox_runtime import MicrosandboxRuntime

log = logging.getLogger("agentlimit.runtime.selector")


class RuntimeSelector:
    """Microsandbox (KVM microVM) is the supported runtime.

    The Docker runtime is an explicit opt-in for development machines without
    /dev/kvm (ALLOW_DOCKER_FALLBACK=true). It is never selected silently, and
    the runtime type is recorded in the ledger and shown on the dashboard.
    """

    @staticmethod
    async def create(session_id: str, workspace_path: Path | None = None, memory_mb: int | None = None):
        try:
            rt = MicrosandboxRuntime(session_id, memory_mb=memory_mb)
            await rt.start(workspace_path)
            log.info("[%s] runtime=microsandbox", session_id)
            return rt, "microsandbox"
        except Exception as e:
            if not settings.allow_docker_fallback:
                raise RuntimeError(f"microsandbox unavailable and docker fallback disabled: {e}") from e
            log.warning("[%s] microsandbox failed (%s); ALLOW_DOCKER_FALLBACK=true -> docker", session_id, e)
            rt = DockerFallbackRuntime(session_id, memory_mb=memory_mb)
            await rt.start(workspace_path)
            return rt, "docker_fallback"
