"""
Enforcement controller.

KILL is a coordinated, verified termination of the WHOLE agent — not just the
sandbox. AgentLimit's agent loop and inference adapter run in the trusted
backend; only generated code runs in the microVM. Killing the microVM alone
would leave the host-side loop free to keep spending inference. So kill():

  1. marks the session permanently terminated,
  2. revokes the inference adapter (every later chat() raises before any
     network I/O) and cancels the orchestrator task,
  3. writes cgroup.kill on the session cgroup and waits for 'populated 0',
  4. stops/removes the sandbox through the runtime as a second path,
  5. VERIFIES each condition independently and returns a TerminationResult.

Success is only reported when every check passes. Otherwise the result is
KILL_FAILED with the failing checks named — never a silent "killed: true".

PAUSE freezes the cgroup (kernel freezer) AND holds the orchestrator at the
next step boundary, so a frozen VM cannot coexist with a host loop that keeps
calling the model.
"""

import asyncio
import logging
from enum import Enum

from backend.config import settings
from backend.orchestrator import model_registry

from backend.evidence.ledger import EvidenceLedger
from backend.governance.resource_policy import MEMORY_HIGH_FRACTION, THROTTLE_CPU_QUOTA_USEC, page_align
from backend.sandbox.cgroup_controller import CgroupController, proc_starttime

log = logging.getLogger("agentlimit.interventions")


class PauseMode(str, Enum):
    KERNEL_FREEZE = "KERNEL_FREEZE"
    STEP_BOUNDARY = "STEP_BOUNDARY"


class KillVerdict(str, Enum):
    KILL_CONFIRMED = "KILL_CONFIRMED"
    KILL_FAILED = "KILL_FAILED"


def _pid_alive(pid: int | None, starttime: int | None = None) -> bool:
    """True only if THIS process (same start time) still runs. A reused PID
    with a different start time is a different process: the original is gone."""
    if not pid:
        return False
    st = proc_starttime(pid)
    if st is None:
        return False
    return starttime is None or st == starttime


class InterventionController:
    def __init__(self, session_id, cg: CgroupController, ledger: EvidenceLedger,
                 orchestrator, budget, runtime=None, sandbox_pid: int | None = None,
                 sandbox_starttime: int | None = None, memory_plan: dict | None = None):
        self.session_id = session_id
        self.cg = cg
        self.ledger = ledger
        self.orchestrator = orchestrator
        self.budget = budget
        self.runtime = runtime
        self.sandbox_pid = sandbox_pid
        self.sandbox_starttime = sandbox_starttime
        self.memory_plan = memory_plan
        self.throttled = False
        self.paused_mode: PauseMode | None = None
        self.killed = False
        self.termination: dict | None = None

    # ---------- THROTTLE ----------

    async def throttle(self, cpu_quota_usec: int = THROTTLE_CPU_QUOTA_USEC) -> None:
        try:
            self.cg.set_cpu_quota(cpu_quota_usec)
            action = f"cpu.max={cpu_quota_usec}/100000"
            requested_ok = True
            ok = self.cg.cpu_max().startswith(str(cpu_quota_usec))   # kernel readback
        except Exception as e:
            action = f"cpu_throttle_failed:{e}"
            requested_ok = False
            ok = False
        self.throttled = ok          # only true when the kernel readback confirms it
        inf = getattr(self.orchestrator, "inference", None)
        downgrade = bool(inf and inf.downgrade_available)
        self.ledger.append("throttle_applied", {
            "action": action,
            "requested": True,
            "applied": requested_ok,
            "verified": ok,
            "model_downgrade": ({"from": settings.vultr_model_default, "to": inf.cheap_model} if downgrade
                                else f"unavailable ({model_registry.STATE.get('downgrade_reason')}) — CPU throttling only"),
            "cpu_max_readback": self.cg.cpu_max(),
            "note": "cpu.max restricts CPU bandwidth; cumulative CPU use is tracked separately against the budget.",
        })
        if self.orchestrator is not None:
            try:
                await self.orchestrator.set_throttle(True)
            except Exception as e:
                self.ledger.append("throttle_model_failed", {"error": str(e)})

    # ---------- RECOMMENDATION ACTIONS ----------

    async def apply_recommendation(self, rec: dict) -> bool:
        action = rec.get("action", "")
        detail: dict = {"recommendation_id": rec.get("recommendation_id"),
                        "rule_id": rec.get("rule_id", ""), "action": action}
        applied = False
        try:
            if action == "stop_identical_retries" and self.orchestrator is not None:
                self.orchestrator.flag_stop_retries(reason=rec.get("rule_id", "retry_loop"))
                applied = True
            elif action == "compact_context" and self.orchestrator is not None:
                before, after = await self.orchestrator.compact_context()
                detail["context_chars_before"] = before
                detail["context_chars_after"] = after
                applied = True
            elif action == "set_memory_high":
                # Same boundary as the policy: host-accounted VMM memory.
                # memory.high (reclaim pressure) sits BELOW memory.max (= budget).
                soft = page_align((self.memory_plan or {}).get("memory_high_bytes") or
                                  MEMORY_HIGH_FRACTION * self.budget.memory_limit_bytes)
                self.cg.set_memory_high(soft)
                detail["memory.high_bytes"] = soft
                detail["memory_budget_bytes"] = self.budget.memory_limit_bytes
                detail["boundary"] = "host-accounted VMM memory (guest RAM + VMM overhead)"
                applied = True
                rb = (self.cg.readback() or {}).get("memory.high")
                detail["memory.high_readback"] = rb
                detail["verified"] = rb == str(soft)
            elif action == "notify_operator":
                detail["note"] = "operator decision required; no automatic action"
            else:
                detail["error"] = "unknown action"
        except Exception as e:
            detail["error"] = str(e)
        detail["applied"] = applied
        # host-side flags are verified by construction; kernel knobs by readback (above)
        detail.setdefault("verified", applied)
        self.ledger.append("recommendation_applied", detail)
        return applied

    # ---------- PAUSE / RESUME ----------

    async def pause(self) -> PauseMode:
        # Always hold the host-side loop: no new model calls or exec while paused.
        if self.orchestrator is not None:
            await self.orchestrator.request_pause()
        frozen = await asyncio.to_thread(self.cg.freeze)
        if frozen:
            self.paused_mode = PauseMode.KERNEL_FREEZE
            self.ledger.append("pause_applied", {
                "mode": PauseMode.KERNEL_FREEZE.value,
                "verified_frozen": self.cg.is_frozen(),
                "orchestrator_held": True,
            })
        else:
            self.paused_mode = PauseMode.STEP_BOUNDARY
            self.ledger.append("pause_applied", {
                "mode": PauseMode.STEP_BOUNDARY.value,
                "verified_frozen": False,
                "orchestrator_held": True,
                "note": "kernel freeze unavailable; paused at next step boundary",
            })
        return self.paused_mode

    async def resume(self, extra_cpu_usec: int = 0) -> bool:
        ok = True
        if self.paused_mode == PauseMode.KERNEL_FREEZE:
            ok = await asyncio.to_thread(self.cg.unfreeze)
        if self.orchestrator is not None:
            await self.orchestrator.resume()

        if extra_cpu_usec > 0:
            self.budget.cpu_budget_usec += extra_cpu_usec
            self.ledger.append("budget_extension", {
                "cpu_usec_added": extra_cpu_usec,
                "new_cpu_budget_usec": self.budget.cpu_budget_usec,
                "approved_by": "operator",
            })

        try:
            self.cg.set_cpu_quota(self.cg.base_cpu_quota_usec)
        except Exception:
            pass
        if self.orchestrator is not None:
            try:
                await self.orchestrator.set_throttle(False)
            except Exception:
                pass

        self.paused_mode = None
        self.throttled = False
        self.ledger.append("resume_applied", {"ok": ok, "verified_unfrozen": not self.cg.is_frozen()})
        return ok

    # ---------- KILL ----------

    async def kill(self, reason: str = "hard_limit") -> dict:
        if self.termination is not None and self.termination.get("verdict") == KillVerdict.KILL_CONFIRMED:
            self.ledger.append("kill_requested", {"reason": reason, "note": "already terminated"})
            return self.termination

        self.ledger.append("kill_requested", {"reason": reason, "method": "revoke+cancel+cgroup.kill"})
        self.killed = True

        # 1+2. Terminate the host-side agent: revoke inference, cancel the loop.
        if self.orchestrator is not None:
            try:
                await self.orchestrator.terminate(reason)
            except Exception as e:
                self.ledger.append("orchestrator_terminate_error", {"error": str(e)})

        # 3. Kernel kill of the whole sandbox process tree.
        cgroup_kill_ok = await asyncio.to_thread(self.cg.kill, 3.0)

        # 4. Second path: stop and remove the sandbox through the runtime SDK.
        runtime_result = {"stop_ok": None, "removed": None, "note": "no runtime attached"}
        if self.runtime is not None:
            try:
                runtime_result = await asyncio.wait_for(self.runtime.stop(force=True), timeout=8.0) or {}
            except Exception as e:
                runtime_result = {"stop_ok": False, "removed": False, "stop_error": f"{type(e).__name__}: {e}"[:300]}
            if not runtime_result.get("removed"):
                self.ledger.append("runtime_stop_error", runtime_result)

        # 5. Independent verification. Every check is reported separately.
        agent_task_done = True
        if self.orchestrator is not None:
            agent_task_done = await self.orchestrator.wait_terminated(timeout=3.0)
        inference = getattr(self.orchestrator, "inference", None)
        checks = {
            "session_marked_terminated": bool(getattr(self.orchestrator, "terminated", True)),
            "inference_revoked": bool(getattr(inference, "revoked", True)),
            "agent_task_finished": agent_task_done,
            "cgroup_populated_0": not self.cg.is_populated(),
            "sandbox_pid_gone": not _pid_alive(self.sandbox_pid, self.sandbox_starttime),
            "sandbox_removed_via_runtime": bool(runtime_result.get("removed")) if self.runtime is not None else True,
        }
        verdict = KillVerdict.KILL_CONFIRMED if all(checks.values()) else KillVerdict.KILL_FAILED
        self.termination = {
            "verdict": verdict.value,
            "reason": reason,
            "checks": checks,
            "cgroup_kill_wrote": cgroup_kill_ok,
            "runtime_stop": runtime_result,
            "inference_at_kill": inference.inflight_summary() if inference is not None else None,
            "sandbox_pid": self.sandbox_pid,
            "final_cpu_usec": self.cg.cpu_usage_usec(),
            "final_memory_peak_bytes": self.cg.memory_peak(),
            "final_tokens": self.orchestrator.inference.total_tokens() if self.orchestrator else None,
        }
        self.ledger.append("kill_verified" if verdict == KillVerdict.KILL_CONFIRMED else "kill_failed",
                           self.termination)
        log.info("[%s] kill verdict=%s checks=%s", self.session_id, verdict.value, checks)
        return self.termination
