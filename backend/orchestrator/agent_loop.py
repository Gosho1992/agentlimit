"""
Multi-step agent loop (host-side, trusted control plane).

Architecture, stated precisely: this loop and the inference adapter run in
the AgentLimit backend on the Vultr VM. Only the model-generated Python runs
inside the Microsandbox microVM. The external governor can therefore stop the
agent at three points it controls — the model (revoke), the loop (cancel /
step gate), and the sandbox (cgroup freeze/kill) — without the agent's
cooperation.

Per step:
  1. governor gate (may deny: paused, retry loop, terminated)
  2. model call through the ContextManager (compaction is real)
  3. governor gate again (a kill/pause may have landed during the call)
  4. execute in the sandbox, record result, hashes, per-step token delta
"""

import asyncio
import hashlib
import logging
from pathlib import Path

from backend.orchestrator.code_generator import CodeGenerator, EmptyGeneration
from backend.orchestrator.context_manager import ContextManager
from backend.orchestrator.inference_adapter import BudgetExceeded, InferenceAdapter, InferenceRevoked

log = logging.getLogger("agentlimit.agent")

AGENT_SELF_RETRY_LIMIT = 5   # the agent's own backstop; the governor acts at 3


def _clip(text: str, head: int = 1000, tail: int = 1000) -> str:
    """Keep the start AND the end (where the actual exception is) of long output."""
    text = text or ""
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}\n…[{len(text) - head - tail} chars omitted]…\n{text[-tail:]}"


class AgentOrchestrator:
    def __init__(self, session_id: str, task: str, ledger, budget, runtime=None,
                 artifact_dir: str | Path | None = None, max_steps: int = 6, inference=None):
        self.session_id = session_id
        self.task = task
        self.ledger = ledger
        self.budget = budget
        self.runtime = runtime
        self.max_steps = max_steps
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        self.inference = inference or InferenceAdapter(
            token_limit=budget.token_limit, cost_limit_usd=getattr(budget, "cost_limit_usd", 0.0))
        self.inference.on_call = lambda rec: self.ledger.append("inference_call", rec)
        self.context = ContextManager()
        self.generator = CodeGenerator(self.inference, self.context)

        self.tokens_median = 0
        self.tokens_last_step = 0
        self.consecutive_identical_errors = 0
        self.last_error_signature = ""
        self._steps: list[int] = []          # per-step token DELTAS
        self.step_count = 0
        self.final_output: dict | None = None
        self.status = "pending"
        self.running = False

        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._stop_retries = False
        self._stop_reason = ""
        self.terminated = False
        self.terminate_reason = ""
        self._task: asyncio.Task | None = None
        self.pre_step_gate = None            # async () -> (bool, str), installed by the governor

    # ---------- lifecycle ----------

    def attach_task(self, task: asyncio.Task) -> None:
        self._task = task

    async def stop(self):
        if self.runtime is not None:
            await self.runtime.stop()

    # ---------- controls from governance ----------

    async def request_pause(self):
        self._resume_event.clear()
        self.ledger.append("orchestrator_hold", {"held": True})

    async def resume(self):
        self._resume_event.set()
        self.ledger.append("orchestrator_hold", {"held": False})

    async def set_throttle(self, on: bool):
        self.inference.set_throttle(on)
        self.ledger.append("orchestrator_throttle", {
            "on": on,
            "model_downgrade": bool(on and self.inference.downgrade_available),
            "model_now": self.inference._effective_model(self.inference.last_model),
        })

    def flag_stop_retries(self, reason: str = "retry_loop"):
        self._stop_retries = True
        self._stop_reason = reason
        self.ledger.append("orchestrator_stop_retries", {"reason": reason})

    async def compact_context(self) -> tuple[int, int]:
        before = ContextManager.size(self.context.build())
        self.context.max_chars = max(2000, self.context.max_chars // 2)
        after = ContextManager.size(self.context.build())
        self.ledger.append("orchestrator_compact_context", {
            "max_chars": self.context.max_chars,
            "prompt_chars_before": before,
            "prompt_chars_after": after,
        })
        return before, after

    async def receive_recommendation(self, rec: dict):
        self.ledger.append("orchestrator_got_recommendation", {"recommendation_id": rec.get("recommendation_id"),
                                                               "rule_id": rec.get("rule_id")})

    async def terminate(self, reason: str):
        """Called by the governor on KILL. Irreversible."""
        self.terminated = True
        self.terminate_reason = reason
        self.inference.revoke(reason)
        self._resume_event.set()  # release a held loop so it can observe termination
        if self._task is not None and not self._task.done() and self._task is not asyncio.current_task():
            self._task.cancel()
        self.ledger.append("orchestrator_terminated", {"reason": reason, "inference_revoked": True})

    async def wait_terminated(self, timeout: float = 3.0) -> bool:
        if self._task is None:
            return not self.running
        if self._task.done():
            return True
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
        return self._task.done()

    async def token_spend(self) -> int:
        return self.inference.total_tokens()

    # ---------- main loop ----------

    async def _gate(self, where: str) -> str | None:
        """Return a stop reason, or None to continue."""
        if not self._resume_event.is_set():
            self.ledger.append("orchestrator_waiting", {"at": where})
            await self._resume_event.wait()
        if self.terminated:
            return "terminated"
        if self._stop_retries:
            return f"stopped_by_governor:{self._stop_reason}"
        if self.pre_step_gate is not None:
            ok, reason = await self.pre_step_gate()
            if not ok:
                return reason
        return None

    async def run(self) -> dict | None:
        self.running = True
        self.status = "running"
        try:
            return await self._run()
        except asyncio.CancelledError:
            self.status = "killed"
            self.ledger.append("agent_cancelled", {"reason": self.terminate_reason or "cancelled"})
            return None
        except InferenceRevoked as e:
            self.status = "killed"
            self.ledger.append("agent_inference_revoked", {"reason": str(e)})
            return None
        except Exception as e:
            self.status = "failed"
            self.ledger.append("agent_crashed", {"error": f"{type(e).__name__}: {e}"[:500]})
            return None
        finally:
            self.running = False

    async def _run(self) -> dict | None:
        self.generator.start(self.task)
        for _ in range(self.max_steps):
            stop = await self._gate("before_model_call")
            if stop:
                return self._stopped(stop)

            self.step_count += 1
            step_id = f"step-{self.step_count}"
            prev_cumulative = self.inference.total_tokens()

            try:
                code = await self.generator.generate()
            except BudgetExceeded as e:
                self.status = "budget_exceeded"
                self.ledger.append("agent_budget_exceeded", {"step_id": step_id, "reason": str(e)})
                return None
            except EmptyGeneration as e:
                self.ledger.append("empty_generation", {"step_id": step_id, "detail": str(e),
                                                        "model": self.inference.last_model})
                self.generator.record_empty(str(e))
                await self._record_step_tokens(prev_cumulative, step_id)
                sig = "empty_generation"
                self.consecutive_identical_errors = self.consecutive_identical_errors + 1 \
                    if self.last_error_signature == sig else 1
                self.last_error_signature = sig
                if self.consecutive_identical_errors >= AGENT_SELF_RETRY_LIMIT:
                    return self._stopped("agent_self_retry_limit")
                continue

            code_sha = hashlib.sha256(code.encode()).hexdigest()
            self.ledger.append("code_generated", {
                "step_id": step_id,
                "code_sha256": code_sha,
                "code_length_bytes": len(code),
                "prompt_chars": self.generator.last_prompt_chars,
                "model": self.inference.last_model,
            })

            stop = await self._gate("before_sandbox_exec")
            if stop:
                await self._record_step_tokens(prev_cumulative, step_id)
                return self._stopped(stop)

            try:
                result = await self.runtime.exec(code, timeout=90.0)
            except Exception as e:
                self.ledger.append("step_exception", {"step_id": step_id, "error": str(e)[:1000]})
                self.generator.record_exception(code, str(e))
                await self._record_step_tokens(prev_cumulative, step_id)
                continue

            self.ledger.append("step_result", {
                "step_id": step_id,
                "exit_code": result["exit_code"],
                "stdout": _clip(result["stdout"]),
                "stderr": _clip(result["stderr"]),
                "stdout_sha256": hashlib.sha256(result["stdout"].encode()).hexdigest(),
            })
            await self._record_step_tokens(prev_cumulative, step_id)

            if result["exit_code"] == 0 and result["stdout"].strip() and not self.terminated:
                return self._complete(step_id, result["stdout"])
            if result["exit_code"] == 0 and not result["stdout"].strip():
                # exit 0 with no output is not a result: treat as a failed attempt
                result = {**result, "stderr": (result["stderr"] or "") +
                          "\n[agentlimit] the program exited 0 but printed nothing to stdout; "
                          "print the final result."}

            err_sig = (result["stderr"] or "").strip()[-200:]
            if err_sig and err_sig == self.last_error_signature:
                self.consecutive_identical_errors += 1
            else:
                self.consecutive_identical_errors = 1
            self.last_error_signature = err_sig
            self.generator.record_result(code, result)

            if self.consecutive_identical_errors >= AGENT_SELF_RETRY_LIMIT:
                return self._stopped("agent_self_retry_limit")

        self.status = "exhausted_steps"
        self.ledger.append("agent_exhausted_steps", {"steps": self.step_count})
        return None

    def _stopped(self, reason: str) -> None:
        self.status = "killed" if reason == "terminated" else "stopped"
        self.ledger.append("agent_stopped", {
            "reason": reason,
            "step": self.step_count,
            "consecutive_identical_errors": self.consecutive_identical_errors,
            "stopped_by": "governor" if reason != "agent_self_retry_limit" else "agent",
        })
        return None

    def _complete(self, step_id: str, stdout: str) -> dict:
        out_bytes = stdout.encode()
        out_sha = hashlib.sha256(out_bytes).hexdigest()
        artifact_path = None
        if self.artifact_dir is not None:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            p = self.artifact_dir / f"{self.session_id}_final_stdout.txt"
            p.write_bytes(out_bytes)
            artifact_path = str(p)
        self.final_output = {
            "step_id": step_id,
            "stdout": stdout,
            "stdout_sha256": out_sha,
            "stdout_bytes": len(out_bytes),
            "artifact_path": artifact_path,
            "tokens_used": self.inference.total_tokens(),
            "inference": self.inference.snapshot(),
        }
        self.status = "completed"
        self.ledger.append("agent_completed", {
            "step_id": step_id,
            "stdout_sha256": out_sha,
            "stdout_bytes": len(out_bytes),
            "artifact_relative_path": f"artifacts/{self.session_id}_final_stdout.txt" if artifact_path else None,
            "total_tokens": self.inference.total_tokens(),
            "estimated_cost_usd": round(self.inference.total_cost_usd, 6),
        })
        return self.final_output

    async def _record_step_tokens(self, prev_cumulative: int, step_id: str):
        delta = self.inference.total_tokens() - prev_cumulative
        prior = list(self._steps)
        self._steps.append(delta)
        self.tokens_last_step = delta
        if len(prior) >= 2:
            recent = sorted(prior[-5:])
            self.tokens_median = recent[len(recent) // 2]
        self.ledger.append("step_tokens", {
            "step_id": step_id,
            "token_delta": delta,
            "cumulative_tokens": self.inference.total_tokens(),
            "median_prior_step_tokens": self.tokens_median,
        })
