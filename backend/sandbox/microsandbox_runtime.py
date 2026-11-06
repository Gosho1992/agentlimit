"""
Microsandbox (libkrun / KVM microVM) runtime — written against the
microsandbox Python SDK 0.7.x API:

    sb  = await Sandbox.create(name, image=..., memory=<MiB>, cpus=..., network=Network.none(), ...)
    out = await sb.exec("python3", ["-c", code], timeout=..., rlimits=[...])
    out.exit_code / out.stdout_text / out.stderr_text
    await sb.fs.write(path, bytes)
    m   = await sb.metrics()        # vcpu_time_ns, memory_bytes (guest view)
    await sb.stop() / await sb.kill(); await Sandbox.remove(name)

Containment properties this class sets explicitly (and the containment
probe verifies at session start):
  - Network.none(): egress and ingress denied
  - env={}: no host environment and no API keys enter the guest
  - per-exec timeout and guest rlimits (process count, file size)
  - one fresh microVM per session, removed when the session closes
"""

import logging
from pathlib import Path

from backend.config import settings

try:
    from microsandbox import Network, Rlimit, RlimitResource, Sandbox  # type: ignore
    try:
        from microsandbox import ExecTimeoutError  # type: ignore
    except ImportError:  # pragma: no cover
        ExecTimeoutError = TimeoutError  # type: ignore
except ImportError:
    Sandbox = None  # type: ignore
    Network = Rlimit = RlimitResource = None  # type: ignore
    ExecTimeoutError = TimeoutError  # type: ignore

log = logging.getLogger("agentlimit.runtime.microsandbox")

# Probe executed inside the guest. Prints one JSON line.
CONTAINMENT_PROBE = r'''
import json, os, socket
res = {}
try:
    s = socket.create_connection(("1.1.1.1", 443), timeout=3); s.close(); res["egress_blocked"] = False
except Exception as e:
    res["egress_blocked"] = True; res["egress_error"] = type(e).__name__
try:
    socket.getaddrinfo("api.vultrinference.com", 443); res["dns_blocked"] = False
except Exception:
    res["dns_blocked"] = True
keys = [k for k in os.environ if any(t in k.upper() for t in ("KEY", "TOKEN", "SECRET", "PASSWORD", "VULTR"))]
res["secret_env_vars"] = keys
res["no_secrets_in_env"] = not keys
res["host_paths_visible"] = [p for p in ("/var/lib/agentlimit", "/sys/fs/cgroup/agentlimit", "/root/.ssh") if os.path.exists(p)]
res["workspace_files"] = sorted(os.listdir("/workspace")) if os.path.isdir("/workspace") else []
res["uid"] = os.getuid()
try:
    target = os.path.join("/workspace", res["workspace_files"][0]) if res["workspace_files"] else "/workspace/.probe"
    open(target, "a").close()
    res["inputs_read_only"] = False
except Exception:
    res["inputs_read_only"] = True
print(json.dumps(res))
'''


class MicrosandboxRuntime:
    runtime_type = "microsandbox"

    def __init__(self, session_id: str, memory_mb: int | None = None):
        self.session_id = session_id
        self.name = f"agentlimit-{session_id[:12]}"
        self.memory_mb = memory_mb or settings.microsandbox_memory_mb
        self._sandbox = None
        self._stop_result: dict | None = None

    async def start(self, workspace_path: Path | None = None):
        if Sandbox is None:
            raise RuntimeError("microsandbox SDK not installed (pip install microsandbox)")
        self._sandbox = await Sandbox.create(
            self.name,
            image=settings.microsandbox_image,
            memory=self.memory_mb,
            cpus=settings.microsandbox_cpus,
            network=Network.none(),
            env={},
            labels={"app": "agentlimit", "session": self.session_id},
            replace=True,
        )
        await self._sandbox.fs.mkdir("/workspace")
        if workspace_path and workspace_path.exists():
            await self._seed_workspace(workspace_path)
        # Inputs are root-owned and read-only; agent code runs as an
        # unprivileged user (settings.sandbox_exec_user), so it cannot alter them.
        await self._sandbox.exec("sh", ["-c", "chmod 0444 /workspace/* 2>/dev/null; chmod 0555 /workspace"])
        return self._sandbox

    async def _seed_workspace(self, host_dir: Path):
        for f in sorted(host_dir.iterdir()):
            if f.is_file():
                await self._sandbox.fs.write(f"/workspace/{f.name}", f.read_bytes())
                log.info("[%s] seeded /workspace/%s (%d bytes)", self.session_id, f.name, f.stat().st_size)

    def _rlimits(self):
        if Rlimit is None:
            return None
        return [
            Rlimit(RlimitResource.NPROC, 64, 64),            # guest-side process cap
            Rlimit(RlimitResource.FSIZE, 64 * 2**20, 64 * 2**20),  # 64 MB max file write
        ]

    async def exec(self, code: str, timeout: float = 60.0) -> dict:
        if self._sandbox is None:
            raise RuntimeError("sandbox not started")
        try:
            user = settings.sandbox_exec_user or None
            out = await self._sandbox.exec("python3", ["-c", code], cwd="/tmp" if user else "/workspace",
                                           user=user, env={"HOME": "/tmp"},
                                           timeout=timeout, rlimits=self._rlimits())
        except ExecTimeoutError:
            return {"stdout": "", "stderr": f"timeout after {timeout}s", "exit_code": -9}
        return {
            "stdout": out.stdout_text or "",
            "stderr": out.stderr_text or "",
            "exit_code": out.exit_code,
        }

    async def containment_probe(self) -> dict:
        import json
        r = await self.exec(CONTAINMENT_PROBE, timeout=20)
        try:
            return json.loads(r["stdout"].strip().splitlines()[-1])
        except Exception:
            return {"probe_error": (r.get("stderr") or r.get("stdout") or "")[:300]}

    async def metrics(self) -> dict | None:
        """Runtime-reported (guest-side) metrics, used only as a cross-check of
        the host cgroup numbers. Enforcement never relies on these."""
        if self._sandbox is None:
            return None
        try:
            m = await self._sandbox.metrics()
            return {"vcpu_time_usec": int(m.vcpu_time_ns // 1000), "guest_memory_bytes": int(m.memory_bytes)}
        except Exception:
            return None

    async def stop(self, force: bool = False) -> dict:
        """Stop and remove the sandbox. Returns the ACTUAL outcome:
        {"stop_ok", "removed", "stop_error", "remove_error"}. Idempotent."""
        import asyncio
        if self._stop_result and self._stop_result.get("removed"):
            return self._stop_result
        res = {"stop_ok": False, "removed": False, "stop_error": None, "remove_error": None}
        sb, self._sandbox = self._sandbox, None
        if sb is not None:
            try:
                if force:
                    await sb.kill(timeout=3)
                else:
                    await sb.stop(timeout=5)
                res["stop_ok"] = True
            except Exception as e:  # e.g. VMM already gone after cgroup.kill
                res["stop_error"] = f"{type(e).__name__}: {e}"[:300]
        if Sandbox is not None:
            for _ in range(6):
                try:
                    await Sandbox.remove(self.name)
                    res["removed"] = True
                    break
                except Exception as e:
                    res["remove_error"] = f"{type(e).__name__}: {e}"[:300]
                    await asyncio.sleep(0.5)
        if res["removed"]:
            # A removed sandbox cannot be running, even if the stop call itself
            # errored because the VMM was already dead.
            res["stop_ok"] = True
            res["remove_error"] = None
        self._stop_result = res
        return res
