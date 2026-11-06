"""
Session lifecycle.

create():
  1. validate the budget (explicit thresholds; never None)
  2. snapshot + SHA-256 the input files (the verifier later checks against
     exactly these bytes)
  3. create the session cgroup with limits
  4. boot a fresh Microsandbox microVM (no network, no env, seeded inputs)
  5. FAIL-CLOSED: find the VMM's host PID, attach its whole tree to the
     cgroup, verify membership — or refuse to run
  6. containment probe inside the guest (egress blocked, no secrets in env)
  7. build orchestrator + governor; the governor gates every agent step

Every session — completed, failed, stopped, killed — goes through finalize():
engine stopped, sandbox stopped and removed, cgroup emptied (verified) and
removed, `session_closed` written with the lifecycle checks.
"""

import asyncio
import hashlib
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from backend.config import settings
from backend.evidence.ledger import EvidenceLedger
from backend.evidence.signer import Signer
from backend.governance.budget import SessionBudget
from backend.governance.engine import GovernanceEngine
from backend.orchestrator.agent_loop import AgentOrchestrator
from backend.sandbox.cgroup_controller import CgroupController
from backend.sandbox.host_process import find_candidates, find_sandbox_pid
from backend.sandbox.runtime_selector import RuntimeSelector

log = logging.getLogger("agentlimit.session_manager")

# MEMORY BOUNDARY: the session memory budget is HOST-ACCOUNTED VMM memory
# (guest RAM + VMM overhead), because that is what the governor measures
# (cgroup memory.current/peak). The guest is given budget - headroom of RAM,
# memory.max = budget (hard), memory.high = 90% of budget (applied by the
# memory_pressure recommendation), and the rule fires at 80% of budget.
MIN_GUEST_MB = 128
# pids.max counts host THREADS of the VMM (vCPU, virtio, async runtime);
# guest process count is limited separately with guest rlimits.
VMM_PIDS_MAX = 512


class SandboxAttachError(RuntimeError):
    """Raised when the sandbox cannot be placed under cgroup control."""


class LimitExceeded(RuntimeError):
    """Raised when a global abuse/spend limit would be exceeded."""


@dataclass
class SessionRuntime:
    session_id: str
    task: str
    cgroup: CgroupController
    ledger: EvidenceLedger
    orchestrator: AgentOrchestrator
    engine: GovernanceEngine
    budget: SessionBudget
    runtime_type: str
    runtime: object
    sandbox_pid: int | None
    inputs: dict
    inputs_dir: Path
    containment: dict
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_ts: float = field(default_factory=lambda: __import__("time").time())
    engine_task: asyncio.Task | None = None
    agent_task: asyncio.Task | None = None
    closed: bool = False
    lifecycle: dict | None = None


def snapshot_inputs(src: Path, dst: Path) -> dict:
    """Copy input files into a session-owned read-only dir and hash them."""
    dst.mkdir(parents=True, exist_ok=True)
    manifest = {}
    if src.exists():
        for f in sorted(src.iterdir()):
            if not f.is_file():
                continue
            target = dst / f.name
            shutil.copy2(f, target)
            target.chmod(0o444)
            manifest[f.name] = {
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "bytes": target.stat().st_size,
            }
    return manifest


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, SessionRuntime] = {}
        self._created_at: list[float] = []
        self._signer = Signer(settings.signer_key_path)

    @property
    def signer(self) -> Signer:
        return self._signer

    def list(self) -> list[SessionRuntime]:
        return list(self._sessions.values())

    async def create(self, task: str, budget: SessionBudget) -> SessionRuntime:
        budget.validate()
        if budget.memory_limit_bytes // (1024 * 1024) - settings.vmm_memory_headroom_mb < MIN_GUEST_MB:
            raise ValueError(f"memory budget must be at least {MIN_GUEST_MB + settings.vmm_memory_headroom_mb} MB "
                             f"(guest {MIN_GUEST_MB} MB + VMM headroom {settings.vmm_memory_headroom_mb} MB)")
        self._check_abuse_limits(budget)
        session_id = uuid.uuid4().hex[:16]
        evidence_root = Path(settings.ledger_dir)
        session_dir = evidence_root / session_id
        workspace_src = Path(os.environ.get("AGENTLIMIT_WORKSPACE", settings.workspace_dir))

        ledger = EvidenceLedger(session_id, self._signer, settings.ledger_dir)
        ledger.append("session_created", {"task": task, "budget": budget.to_dict(),
                                          "public_key_b64": self._signer.public_key_b64()})

        inputs_dir = session_dir / "inputs"
        inputs = snapshot_inputs(workspace_src, inputs_dir)
        ledger.append("inputs_snapshotted", {"files": inputs, "snapshot_dir": str(inputs_dir)})

        cg = CgroupController(session_id, root=settings.cgroup_root)
        cg.set_cpu_quota(cg.base_cpu_quota_usec)
        cg.set_memory_max(budget.memory_limit_bytes)
        cg.set_pids_max(VMM_PIDS_MAX)
        ledger.append("cgroup_configured", {"path": str(cg.path), "cpu.max": cg.cpu_max(),
                                            "memory.max_bytes": cg.memory_max_bytes,
                                            "memory_boundary": "host-accounted VMM (guest RAM + overhead)",
                                            "pids.max": VMM_PIDS_MAX})

        guest_mb = budget.memory_limit_bytes // (1024 * 1024) - settings.vmm_memory_headroom_mb
        runtime, runtime_type = await RuntimeSelector.create(session_id, inputs_dir, memory_mb=guest_mb)
        ledger.append("runtime_started", {"runtime_type": runtime_type,
                                          "sandbox_name": getattr(runtime, "name", ""),
                                          "guest_memory_mb": guest_mb,
                                          "network": "none", "env": "empty"})

        # FAIL-CLOSED attach
        pid = None
        try:
            if hasattr(runtime, "host_pid"):
                pid = await runtime.host_pid()
            if not pid:
                for _ in range(20):  # the VMM may take a moment to appear
                    pid = find_sandbox_pid(getattr(runtime, "name", session_id))
                    if pid:
                        break
                    await asyncio.sleep(0.1)
            if not pid:
                raise SandboxAttachError("sandbox host PID not found; candidates="
                                         f"{find_candidates(getattr(runtime, 'name', session_id))}")
            attached = cg.attach_tree(pid)
            if attached == 0 or not cg.contains(pid):
                raise SandboxAttachError(f"PID {pid} not in session cgroup after attach")
            ledger.append("sandbox_attached", {"pid": pid, "processes_attached": attached,
                                               "cgroup": str(cg.path), "runtime_type": runtime_type})
        except Exception as e:
            ledger.append("sandbox_attach_failed", {"error": str(e)[:500], "fail_closed": True})
            try:
                await runtime.stop(force=True)
            except Exception:
                pass
            cg.kill()
            cg.destroy()
            raise SandboxAttachError(str(e)) from e

        # Containment probe (shown in the dashboard and the evidence report)
        try:
            probe = await runtime.containment_probe()
        except Exception as e:
            probe = {"probe_error": str(e)[:300]}
        probe["passed"] = bool(probe.get("egress_blocked")) and bool(probe.get("no_secrets_in_env")) \
            and not probe.get("host_paths_visible")
        ledger.append("containment_probe", probe)
        if not probe["passed"] and settings.require_containment_probe:
            await runtime.stop(force=True)
            cg.kill()
            cg.destroy()
            raise SandboxAttachError(f"containment probe failed: {probe}")

        orch = AgentOrchestrator(session_id, task, ledger, budget, runtime=runtime,
                                 artifact_dir=session_dir / "artifacts")
        engine = GovernanceEngine(
            session_id=session_id, cg=cg, ledger=ledger, budget=budget, orchestrator=orch,
            poll_interval_s=settings.poll_interval_ms / 1000.0, token_spend_fn=orch.token_spend,
            pause_timeout_s=settings.pause_timeout_s, runtime=runtime, sandbox_pid=pid)
        orch.pre_step_gate = engine.pre_step_check

        rt = SessionRuntime(session_id=session_id, task=task, cgroup=cg, ledger=ledger,
                            orchestrator=orch, engine=engine, budget=budget, runtime_type=runtime_type,
                            runtime=runtime, sandbox_pid=pid, inputs=inputs, inputs_dir=inputs_dir,
                            containment=probe)
        self._sessions[session_id] = rt
        return rt

    def _check_abuse_limits(self, budget) -> None:
        """Bound what a public URL can spend, across ALL sessions."""
        import time as _t
        now = _t.time()
        active = sum(1 for r in self._sessions.values() if not r.closed)
        if active >= settings.max_concurrent_sessions:
            raise LimitExceeded(f"{active} sessions already running (limit {settings.max_concurrent_sessions})")
        recent = [t for t in self._created_at if now - t < 3600]
        if len(recent) >= settings.max_sessions_per_hour:
            raise LimitExceeded(f"hourly session limit reached ({settings.max_sessions_per_hour}/h)")
        day_tokens = sum(r.orchestrator.inference.total_tokens() + r.orchestrator.inference.reserved_tokens()
                         for r in self._sessions.values() if now - r.created_ts < 86400)
        if day_tokens + budget.token_limit > settings.max_tokens_per_day:
            raise LimitExceeded(f"daily token cap reached ({day_tokens:,} used + {budget.token_limit:,} requested "
                                f"> {settings.max_tokens_per_day:,})")
        self._created_at = recent + [now]

    def get(self, session_id: str) -> SessionRuntime | None:
        return self._sessions.get(session_id)

    async def start_run(self, session_id: str):
        rt = self._sessions[session_id]
        rt.engine_task = asyncio.create_task(rt.engine.run())
        rt.agent_task = asyncio.create_task(rt.orchestrator.run())
        rt.orchestrator.attach_task(rt.agent_task)

        async def _watch():
            try:
                await asyncio.wait({rt.agent_task, rt.engine_task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                await self.finalize(session_id)

        asyncio.create_task(_watch())

    async def finalize(self, session_id: str) -> dict:
        rt = self._sessions.get(session_id)
        if rt is None or rt.closed:
            return rt.lifecycle if rt else {}
        rt.closed = True

        # Agent done (or governor killed it). Stop the agent if still running.
        if rt.agent_task and not rt.agent_task.done():
            await rt.orchestrator.terminate("session_finalize")
            await rt.orchestrator.wait_terminated(timeout=3.0)
        rt.orchestrator.inference.revoke("session closed")

        rt.engine.stop_event.set()
        if rt.engine_task and not rt.engine_task.done():
            try:
                await asyncio.wait_for(rt.engine_task, timeout=3.0)
            except Exception:
                pass

        try:
            stop = await asyncio.wait_for(rt.runtime.stop(force=True), timeout=10.0) or {}
        except Exception as e:
            stop = {"stop_ok": False, "removed": False, "stop_error": f"{type(e).__name__}: {e}"[:300]}
        if not stop.get("removed"):
            rt.ledger.append("runtime_stop_error", stop)
        if rt.cgroup.is_populated():
            await asyncio.to_thread(rt.cgroup.kill, 3.0)
        workload_gone = not rt.cgroup.is_populated()
        cgroup_removed = rt.cgroup.destroy()
        # Let in-flight model requests settle so their real usage is recorded
        # (results are discarded); anything unsettled stays "uncertain".
        inflight = await rt.orchestrator.inference.drain(timeout=settings.inflight_drain_s)

        rt.lifecycle = {
            "agent_status": rt.orchestrator.status,
            "final_state": rt.engine.state.value,
            "kill_verdict": (rt.engine.termination or {}).get("verdict"),
            "sandbox_stopped": bool(stop.get("stop_ok")),
            "sandbox_removed": bool(stop.get("removed")),
            "sandbox_stop_error": stop.get("stop_error") or stop.get("remove_error"),
            "workload_gone": workload_gone,
            "cgroup_removed": cgroup_removed,
            "inference_revoked": rt.orchestrator.inference.revoked,
            "inference_in_flight_at_close": inflight["in_flight"],
            "inference_uncertain_reserved_tokens": inflight["uncertain_reserved_tokens"],
            "inference_settled_after_revoke": inflight["settled_after_revoke"],
            "total_tokens_recorded": rt.orchestrator.inference.total_tokens(),
            "inference": rt.orchestrator.inference.snapshot(),
            "wall_seconds": round(rt.engine.wall_seconds, 2),
        }
        rt.lifecycle["clean"] = all([rt.lifecycle["sandbox_removed"], workload_gone, cgroup_removed,
                                     rt.lifecycle["inference_revoked"]])
        rt.ledger.append("session_closed", rt.lifecycle)
        log.info("[%s] closed %s", session_id, rt.lifecycle)
        return rt.lifecycle

    async def stop(self, session_id: str):
        await self.finalize(session_id)

    async def recover(self) -> dict:
        """On startup: close out sessions a previous process left open, and
        kill/remove any orphaned session cgroups and sandboxes."""
        from backend.evidence.archive import list_archived, read_entries, last
        report = {"interrupted_sessions": [], "orphan_cgroups_removed": [], "orphan_sandboxes_removed": []}
        for sid in list_archived():
            entries = read_entries(sid) or []
            if entries and not last(entries, "session_closed") and not last(entries, "session_interrupted"):
                EvidenceLedger(sid, self._signer, settings.ledger_dir).append(
                    "session_interrupted", {"reason": "server restarted before the session closed"})
                report["interrupted_sessions"].append(sid)
        root = Path(settings.cgroup_root)
        if root.exists():
            for d in root.iterdir():
                if d.is_dir() and (d / "cgroup.procs").exists():
                    try:
                        if (d / "cgroup.procs").read_text().strip():
                            (d / "cgroup.kill").write_text("1")
                            await asyncio.sleep(0.5)
                        d.rmdir()
                        report["orphan_cgroups_removed"].append(d.name)
                    except OSError:
                        pass
        try:
            from microsandbox import Sandbox  # type: ignore
            page = await Sandbox.list_with(labels={"app": "agentlimit"})
            for item in getattr(page, "items", None) or getattr(page, "sandboxes", None) or []:
                name = getattr(item, "name", None)
                if name:
                    try:
                        await Sandbox.remove(name)
                        report["orphan_sandboxes_removed"].append(name)
                    except Exception:
                        pass
        except Exception:
            pass
        log.info("startup recovery: %s", report)
        return report


session_manager = SessionManager()
