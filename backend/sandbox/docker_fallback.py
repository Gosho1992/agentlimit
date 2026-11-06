import asyncio
import logging
from pathlib import Path

log = logging.getLogger("agentlimit.runtime.docker")


class DockerFallbackRuntime:
    runtime_type = "docker_fallback"

    def __init__(self, session_id: str, memory_mb: int | None = None):
        self.session_id = session_id
        self.memory_mb = memory_mb or 512
        self.container_name = f"agentlimit-{session_id[:12]}"
        self.name = self.container_name
        self._started = False

    async def start(self, workspace_path: Path | None = None):
        cmd = [
            "docker", "run", "-d",
            "--name", self.container_name,
            "--memory", f"{self.memory_mb}m",
            "--pids-limit", "64",
            "--read-only", "--tmpfs", "/tmp", "--tmpfs", "/workspace",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--cpus", "1",
            "--network", "none",
            "python:3.11-slim",
            "sleep", "3600",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed: {stderr.decode()[:400]}")
        self._started = True

        if workspace_path and workspace_path.exists():
            await self._seed_workspace(workspace_path)

    async def _seed_workspace(self, host_dir: Path):
        # /workspace is a tmpfs (docker cp cannot write into tmpfs), so stream via stdin.
        for f in sorted(host_dir.iterdir()):
            if not f.is_file():
                continue
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", "-i", self.container_name, "sh", "-c", f"cat > /workspace/{f.name}",
                stdin=asyncio.subprocess.PIPE)
            await proc.communicate(f.read_bytes())
            log.info("[%s] seeded /workspace/%s", self.session_id, f.name)
        await self._exec_docker(["exec", self.container_name, "sh", "-c",
                                 "chmod 0444 /workspace/* && chmod 0555 /workspace"])

    async def host_pid(self) -> int | None:
        """Host PID of the container's init process (docker inspect)."""
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.Pid}}", self.container_name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        try:
            pid = int(out.decode().strip())
            return pid or None
        except ValueError:
            return None

    async def containment_probe(self) -> dict:
        import json
        from backend.sandbox.microsandbox_runtime import CONTAINMENT_PROBE
        r = await self.exec(CONTAINMENT_PROBE, timeout=20)
        try:
            return json.loads(r["stdout"].strip().splitlines()[-1])
        except Exception:
            return {"probe_error": (r.get("stderr") or "")[:300]}

    async def metrics(self) -> dict | None:
        return None

    async def _exec_docker(self, args: list[str]) -> int:
        proc = await asyncio.create_subprocess_exec("docker", *args)
        await proc.wait()
        return proc.returncode or 0

    async def exec(self, code: str, timeout: float = 60.0) -> dict:
        if not self._started:
            raise RuntimeError("container not started")
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-u", "nobody", "-w", "/tmp", self.container_name, "python", "-c", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"stdout": "", "stderr": "timeout", "exit_code": -9}
        return {
            "stdout": stdout.decode(),
            "stderr": stderr.decode(),
            "exit_code": proc.returncode if proc.returncode is not None else -1,
        }

    async def stop(self, force: bool = False) -> dict:
        res = {"stop_ok": False, "removed": False, "stop_error": None, "remove_error": None}
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", self.container_name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        msg = err.decode()
        if proc.returncode == 0 or "No such container" in msg:
            res.update(stop_ok=True, removed=True)
        else:
            res["remove_error"] = msg[:300]
        self._started = False
        return res
