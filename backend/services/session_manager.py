"""
Session lifecycle.

create() is a resource TRANSACTION. Each step records what it allocated; on
any failure every allocated resource is cleaned up independently (one failed
cleanup never skips the others), the real cleanup results are written to the
signed ledger, and the caller gets a short failure category plus the session
id (details stay in the protected ledger and server logs).

  1. validate budget + memory plan
  2. durable global-limit reservation (atomic; survives restarts)
  3. signed ledger
  4. snapshot + SHA-256 the inputs (relative paths inside the session dir)
  5. session cgroup; required controllers verified; limits written AND read back
  6. fresh Microsandbox microVM (no network, empty env, read-only inputs)
  7. FAIL-CLOSED verified attachment of the VMM and all live descendants
  8. canonical containment evaluation (every mandatory check must pass)
  9. orchestrator + governor (governor gates every agent step)

finalize() reports each cleanup condition separately and truthfully.
"""

import asyncio
import hashlib
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from backend.config import settings
from backend.evidence.ledger import EvidenceLedger
from backend.evidence.signer import Signer
from backend.governance.budget import SessionBudget
from backend.governance.containment import evaluate_containment
from backend.governance.engine import GovernanceEngine
from backend.governance.resource_policy import BASE_CPU_QUOTA_USEC, VMM_PIDS_MAX, memory_plan
from backend.orchestrator.agent_loop import AgentOrchestrator
from backend.sandbox.cgroup_controller import CgroupController, CgroupSetupError, proc_starttime
from backend.sandbox.host_process import find_candidates, find_sandbox_pid
from backend.sandbox.runtime_selector import RuntimeSelector
from backend.services.usage_store import LimitExceeded, UsageStore

log = logging.getLogger("agentlimit.session_manager")

__all__ = ["SessionManager", "SessionStartError", "SandboxAttachError", "LimitExceeded", "session_manager"]


class SandboxAttachError(RuntimeError):
    """The sandbox could not be placed under verified cgroup control."""


class ContainmentError(RuntimeError):
    """A mandatory containment check failed."""


class SessionStartError(RuntimeError):
    def __init__(self, category: str, session_id: str, detail: str):
        super().__init__(f"{category}: {detail}")
        self.category = category
        self.session_id = session_id
        self.detail = detail


def _category(e: Exception) -> str:
    if isinstance(e, CgroupSetupError):
        return "cgroup_unavailable"
    if isinstance(e, SandboxAttachError):
        return "sandbox_attach_failed"
    if isinstance(e, ContainmentError):
        return "containment_check_failed"
    if isinstance(e, RuntimeError) and "microsandbox" in str(e).lower():
        return "sandbox_runtime_unavailable"
    return "session_start_failed"


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
    sandbox_starttime: int | None
    inputs: dict
    inputs_dir: Path
    containment: dict
    memory: dict
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_ts: float = field(default_factory=time.time)
    engine_task: asyncio.Task | None = None
    agent_task: asyncio.Task | None = None
    closed: bool = False
    lifecycle: dict | None = None


def snapshot_inputs(src: Path, session_dir: Path) -> dict:
    """Copy inputs into <session_dir>/inputs (read-only) and hash them.
    Paths are recorded RELATIVE to the session dir; the verifier resolves
    them and refuses anything outside it."""
    dst = session_dir / "inputs"
    dst.mkdir(parents=True, exist_ok=True)
    files = {}
    if src.exists():
        for f in sorted(src.iterdir()):
            if not f.is_file():
                continue
            target = dst / f.name
            shutil.copy2(f, target)
            target.chmod(0o444)
            files[f.name] = {
                "relative_path": f"inputs/{f.name}",
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "bytes": target.stat().st_size,
            }
    return {"files": files, "snapshot_time_utc": datetime.now(timezone.utc).isoformat()}


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, SessionRuntime] = {}
        self._signer = Signer(settings.signer_key_path)
        self.usage = UsageStore()

    @property
    def signer(self) -> Signer:
        return self._signer

    def list(self) -> list[SessionRuntime]:
        return list(self._sessions.values())

    def get(self, session_id: str) -> SessionRuntime | None:
        return self._sessions.get(session_id)

    # ------------------------------------------------------------------ create

    async def create(self, task: str, budget: SessionBudget) -> SessionRuntime:
        budget.validate()
        mem = memory_plan(budget.memory_limit_bytes)          # ValueError -> 422
        session_id = uuid.uuid4().hex[:16]
        self.usage.reserve(session_id, budget.token_limit, budget.cost_limit_usd)  # LimitExceeded -> 429

        ledger = cg = runtime = None
        try:
            ledger = EvidenceLedger(session_id, self._signer, settings.ledger_dir)
            ledger.append("session_created", {"task": task, "budget": budget.to_dict(), "memory_plan": mem,
                                              "public_key_b64": self._signer.public_key_b64(),
                                              "public_key_fingerprint": self._signer.fingerprint()})
            session_dir = Path(settings.ledger_dir) / session_id
            src = Path(os.environ.get("AGENTLIMIT_WORKSPACE", settings.workspace_dir))
            manifest = snapshot_inputs(src, session_dir)
            ledger.append("inputs_snapshotted", {"session_id": session_id, **manifest})

            cg = CgroupController(session_id, root=settings.cgroup_root)   # CgroupSetupError
            cg.base_cpu_quota_usec = BASE_CPU_QUOTA_USEC
            cg.set_cpu_quota(BASE_CPU_QUOTA_USEC)
            cg.set_memory_max(mem["host_memory_max_bytes"])
            cg.set_pids_max(VMM_PIDS_MAX)
            readback = cg.readback()
            ledger.append("cgroup_configured", {"path": str(cg.path), "capabilities": cg.capabilities(),
                                                "readback": readback, "expected": {
                                                    "cpu.max": f"{BASE_CPU_QUOTA_USEC} 100000",
                                                    "memory.max": str(mem["host_memory_max_bytes"]),
                                                    "pids.max": str(VMM_PIDS_MAX)},
                                                "memory_boundary": mem["boundary"]})

            runtime, runtime_type, selection = await RuntimeSelector.create(
                session_id, session_dir / "inputs", memory_mb=mem["guest_ram_mb"])
            ledger.append("runtime_started", {"runtime_type": runtime_type, "selection": selection,
                                              "sandbox_name": getattr(runtime, "name", ""),
                                              "guest_memory_mb": mem["guest_ram_mb"],
                                              "network": "none", "env": "empty",
                                              "exec_user": settings.sandbox_exec_user or "root"})

            pid = None
            if hasattr(runtime, "host_pid"):
                pid = await runtime.host_pid()
            for _ in range(30):
                if pid:
                    break
                pid = find_sandbox_pid(getattr(runtime, "name", session_id))
                if not pid:
                    await asyncio.sleep(0.1)
            if not pid:
                raise SandboxAttachError(f"sandbox host PID not found; candidates="
                                         f"{find_candidates(getattr(runtime, 'name', session_id))}")
            starttime = proc_starttime(pid)
            attach = await asyncio.to_thread(cg.attach_tree_verified, pid, starttime)
            ledger.append("sandbox_attached" if attach["passed"] else "sandbox_attach_failed",
                          {**attach, "runtime_type": runtime_type, "cgroup": str(cg.path)})
            if not attach["passed"]:
                raise SandboxAttachError(f"host cgroup attachment incomplete: failures={attach['failures']}")

            try:
                probe = await runtime.containment_probe()
            except Exception as e:
                probe = {"probe_error": f"{type(e).__name__}: {e}"[:300]}
            evaluation = evaluate_containment(probe, expected_files=sorted(manifest["files"]))
            ledger.append("containment_probe", {"raw": probe, **evaluation})
            if not evaluation["passed"]:
                raise ContainmentError(f"failed checks: {evaluation['failed_checks']}")

            orch = AgentOrchestrator(session_id, task, ledger, budget, runtime=runtime,
                                     artifact_dir=session_dir / "artifacts")
            engine = GovernanceEngine(
                session_id=session_id, cg=cg, ledger=ledger, budget=budget, orchestrator=orch,
                poll_interval_s=settings.poll_interval_ms / 1000.0, token_spend_fn=orch.token_spend,
                pause_timeout_s=settings.pause_timeout_s, runtime=runtime, sandbox_pid=pid,
                sandbox_starttime=starttime, memory_plan=mem)
            orch.pre_step_gate = engine.pre_step_check

            rt = SessionRuntime(session_id=session_id, task=task, cgroup=cg, ledger=ledger,
                                orchestrator=orch, engine=engine, budget=budget, runtime_type=runtime_type,
                                runtime=runtime, sandbox_pid=pid, sandbox_starttime=starttime,
                                inputs=manifest, inputs_dir=session_dir / "inputs",
                                containment={"raw": probe, **evaluation}, memory=mem)
            self._sessions[session_id] = rt
            self.usage.set_status(session_id, "running")
            return rt
        except Exception as e:
            cleanup = await self._cleanup_failed_start(runtime, cg)
            category = _category(e)
            log.error("[%s] session start failed (%s): %s cleanup=%s", session_id, category, e, cleanup)
            if ledger is not None:
                try:
                    ledger.append("session_start_failed", {"category": category, "error": str(e)[:800],
                                                           "cleanup": cleanup, "fail_closed": True})
                except Exception:
                    log.exception("could not record start failure")
            self.usage.close(session_id, "failed")
            raise SessionStartError(category, session_id, _public_detail(category, e)) from e

    async def _cleanup_failed_start(self, runtime, cg) -> dict:
        res: dict = {}
        if runtime is not None:
            try:
                res["runtime"] = await asyncio.wait_for(runtime.stop(force=True), timeout=10) or {}
            except Exception as e:
                res["runtime"] = {"stop_ok": False, "removed": False, "error": f"{type(e).__name__}: {e}"[:200]}
        if cg is not None:
            try:
                res["cgroup_kill_ok"] = await asyncio.to_thread(cg.kill, 3.0)
            except Exception as e:
                res["cgroup_kill_ok"] = False
                res["cgroup_kill_error"] = str(e)[:200]
            try:
                res["cgroup_removed"] = cg.destroy()
            except Exception as e:
                res["cgroup_removed"] = False
                res["cgroup_destroy_error"] = str(e)[:200]
        return res

    # ------------------------------------------------------------------ run

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

    # ------------------------------------------------------------------ finalize

    async def finalize(self, session_id: str) -> dict:
        rt = self._sessions.get(session_id)
        if rt is None or rt.closed:
            return rt.lifecycle if rt else {}
        rt.closed = True
        inf = rt.orchestrator.inference
        result: dict = {}

        # each step independent: an exception in one never skips the rest
        async def step(name, coro_fn):
            try:
                return await coro_fn()
            except Exception as e:
                result[f"{name}_error"] = f"{type(e).__name__}: {e}"[:300]
                return None

        async def _stop_agent():
            if rt.agent_task and not rt.agent_task.done():
                await rt.orchestrator.terminate("session_finalize")
            return await rt.orchestrator.wait_terminated(timeout=3.0)
        agent_done = await step("agent", _stop_agent)
        inf.revoke(inf.revoke_reason or "session closed")

        async def _stop_engine():
            rt.engine.stop_event.set()
            if rt.engine_task and not rt.engine_task.done():
                await asyncio.wait_for(rt.engine_task, timeout=3.0)
        await step("engine", _stop_engine)

        stop = await step("runtime", lambda: asyncio.wait_for(rt.runtime.stop(force=True), timeout=10.0)) or {}
        if not stop.get("removed"):
            rt.ledger.append("runtime_stop_error", stop or {"error": result.get("runtime_error")})

        async def _kill_cgroup():
            if rt.cgroup.is_populated():
                await asyncio.to_thread(rt.cgroup.kill, 3.0)
            return not rt.cgroup.is_populated()
        workload_gone = bool(await step("cgroup_kill", _kill_cgroup))

        async def _destroy():
            return rt.cgroup.destroy()
        cgroup_removed = bool(await step("cgroup_destroy", _destroy))

        drained = await step("inference_drain", lambda: inf.drain(timeout=settings.inflight_drain_s)) \
            or inf.inflight_summary()

        lifecycle = {
            "agent_status": rt.orchestrator.status,
            "final_state": rt.engine.state.value,
            "kill_verdict": (rt.engine.termination or {}).get("verdict"),
            "agent_terminated": bool(agent_done),
            "inference_revoked": inf.revoked is True,
            "inference_requests_drained": drained["in_flight"] == 0,
            "sandbox_stopped": stop.get("stop_ok") is True,
            "sandbox_removed": stop.get("removed") is True,
            "sandbox_stop_error": stop.get("stop_error") or stop.get("remove_error") or result.get("runtime_error"),
            "workload_gone": workload_gone,
            "cgroup_removed": cgroup_removed,
            "inference_accounting": "COMPLETE" if drained["uncertain_requests"] == 0 and drained["in_flight"] == 0
                                    else "PARTIAL",
            "inference_uncertain_reserved_tokens": drained["uncertain_reserved_tokens"],
            "inference_uncertain_reserved_cost_usd": round(inf.uncertain_reserved_cost_usd(), 6),
            "inference_settled_after_revoke": drained["settled_after_revoke"],
            "total_tokens_recorded": inf.total_tokens(),
            "inference": inf.snapshot(),
            "wall_seconds": round(rt.engine.wall_seconds, 2),
            **result,
        }
        lifecycle["clean"] = all(lifecycle[k] is True for k in (
            "agent_terminated", "inference_revoked", "sandbox_removed", "workload_gone", "cgroup_removed"))
        rt.lifecycle = lifecycle
        rt.ledger.append("session_closed", lifecycle)
        try:
            self.usage.close(session_id, "closed", actual_tokens=inf.total_tokens(),
                             uncertain_tokens=drained["uncertain_reserved_tokens"],
                             estimated_cost_usd=inf.total_cost_usd,
                             reserved_cost_usd=inf.uncertain_reserved_cost_usd())
        except Exception:
            log.exception("usage close failed")
        log.info("[%s] closed clean=%s", session_id, lifecycle["clean"])
        return lifecycle

    async def stop(self, session_id: str):
        await self.finalize(session_id)

    # ------------------------------------------------------------------ recovery

    async def recover(self) -> dict:
        """On startup: keep interrupted spend in the usage history, close out
        ledgers a previous process left open, and remove orphaned cgroups
        and sandboxes."""
        from backend.evidence.archive import last, list_archived, read_entries
        report = {"usage_interrupted": self.usage.mark_interrupted(), "interrupted_sessions": [],
                  "orphan_cgroups_removed": [], "orphan_cgroups_failed": [], "orphan_sandboxes_removed": []}
        for sid in list_archived():
            entries = read_entries(sid) or []
            if entries and not last(entries, "session_closed") and not last(entries, "session_interrupted") \
                    and not last(entries, "session_start_failed"):
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
                    except OSError as e:
                        report["orphan_cgroups_failed"].append(f"{d.name}: {e}")
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


def _public_detail(category: str, e: Exception) -> str:
    """Short, non-sensitive message for public callers."""
    return {
        "cgroup_unavailable": "required cgroup v2 controllers are unavailable on this host",
        "sandbox_attach_failed": "the microVM could not be verified inside its host cgroup",
        "containment_check_failed": str(e)[:200],
        "sandbox_runtime_unavailable": "the microVM runtime could not start",
    }.get(category, "session could not be started safely")


session_manager = SessionManager()
