"""
External governance engine.

Runs in the trusted backend, outside the microVM. Every POLL interval it reads
host-side cgroup counters (cpu.stat, memory.current/peak), the inference
adapter's token/cost accounting and the wall clock, and:

  - walks UP the ladder NORMAL→ALERT→RECOMMEND→THROTTLE→PAUSE→KILL one state per
    tick (no stage is skipped),
  - evaluates recommendation rules continuously (each rule fires once, and
    re-arms after its condition clears),
  - gates every agent step (pre_step_check) so the retry-loop rule is enforced
    BEFORE the next model call, not after the agent's own breaker,
  - de-escalates only through an explicit operator resume,
  - escalates PAUSE→KILL after pause_timeout_s,
  - on KILL performs a verified, coordinated termination and records
    KILL_CONFIRMED or KILL_FAILED — never an unverified "killed".
"""

import asyncio
import logging
import time
from collections import deque

from backend.evidence.ledger import EvidenceLedger
from backend.governance.interventions import InterventionController, KillVerdict
from backend.governance.recommendation_engine import RecommendationEngine
from backend.governance.thresholds import State, Thresholds, rank
from backend.sandbox.cgroup_controller import CgroupController

log = logging.getLogger("agentlimit.engine")


class GovernanceEngine:
    def __init__(
        self,
        session_id: str,
        cg: CgroupController,
        ledger: EvidenceLedger,
        budget,
        orchestrator,
        poll_interval_s: float = 0.5,
        token_spend_fn=None,
        pause_timeout_s: float = 120.0,
        runtime=None,
        sandbox_pid: int | None = None,
        sandbox_starttime: int | None = None,
        memory_plan: dict | None = None,
        memory_validation_delta_bytes: int = 64 * 1024 * 1024,
    ):
        self.session_id = session_id
        self.cg = cg
        self.ledger = ledger
        self.budget = budget
        self.orchestrator = orchestrator
        self.runtime = runtime
        self.poll_interval_s = poll_interval_s
        self.pause_timeout_s = pause_timeout_s
        self.token_spend_fn = token_spend_fn or _zero

        self.state: State = State.NORMAL
        self.recommender = RecommendationEngine()
        self.interventions = InterventionController(
            session_id, cg, ledger, orchestrator, budget, runtime=runtime, sandbox_pid=sandbox_pid,
            sandbox_starttime=sandbox_starttime, memory_plan=memory_plan)
        # Serialises every state-changing decision (ticks, operator pause/resume/kill).
        # KILL is irreversible and wins; never held across a model call.
        self.transition_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.ticks = 0
        self.tick_errors = 0
        self.last_utilisation: dict[str, float] = {}
        self.recommendations: list[dict] = []
        self._armed_rules: set[str] = set()
        self._cpu_hist: deque[tuple[float, int]] = deque(maxlen=12)
        self._last_runtime_metrics: dict | None = None

        # Memory telemetry gate: host memory is used for enforcement only after
        # we have seen it respond to guest allocation.
        self._memory_validation_delta = memory_validation_delta_bytes
        self._memory_telemetry_valid = False
        self._memory_baseline = 0

        self._session_start = time.monotonic()
        self._frozen: dict = {}
        self._manual_pause = False
        self._manual_kill = False
        self._kill_reason = ""
        self._paused_at: float | None = None

    # ---------- properties ----------

    @property
    def termination(self) -> dict | None:
        return self.interventions.termination

    @property
    def wall_seconds(self) -> float:
        end = self._frozen.get("wall_seconds") if self._frozen else None
        return end if end is not None else time.monotonic() - self._session_start

    def freeze_metrics(self) -> dict:
        """Snapshot the final host measurements before the cgroup is removed,
        so a closed session keeps showing what was actually measured."""
        if not self._frozen:
            self._frozen = {"cpu_usec": self.cg.cpu_usage_usec(), "memory_bytes": self.cg.memory_current(),
                            "memory_peak_bytes": self.cg.memory_peak(),
                            "wall_seconds": time.monotonic() - self._session_start}
        return self._frozen

    # ---------- public API ----------

    async def run(self):
        self.ledger.append("governance_started", {"budget": self._budget_dict(),
                                                  "poll_interval_s": self.poll_interval_s})
        while not self.stop_event.is_set():
            try:
                async with self.transition_lock:
                    if self.state == State.KILL:
                        break
                    await self._tick()
            except Exception as e:
                self.tick_errors += 1
                log.exception("[%s] tick failed: %s", self.session_id, e)
                self.ledger.append("governance_tick_error", {"error": f"{type(e).__name__}: {e}"[:300],
                                                             "count": self.tick_errors})
                if self.tick_errors >= 5:
                    # Fail closed: a governor that cannot govern must stop the agent.
                    self._kill_reason = "governor_fault_fail_closed"
                    async with self.transition_lock:
                        if self.state != State.KILL:
                            await self._transition_to(State.KILL, *self._read())
                    self.stop_event.set()
                    break
            await asyncio.sleep(self.poll_interval_s)
        log.info("[%s] governance loop stopped", self.session_id)

    def force_pause(self):
        self._manual_pause = True
        self.ledger.append("manual_pause", {"by": "operator"})

    def force_kill(self, reason: str = "operator_kill"):
        self._manual_kill = True
        self._kill_reason = reason
        self.ledger.append("manual_kill", {"by": "operator", "reason": reason})

    async def kill_now(self, reason: str = "operator_kill") -> dict:
        """Synchronous (awaitable) kill used by the API so the HTTP response can
        carry the verified verdict."""
        self.force_kill(reason)
        async with self.transition_lock:          # KILL overrides any in-progress decision
            if self.state != State.KILL:
                await self._transition_to(State.KILL, *self._read())
        self.stop_event.set()
        return self.termination or {}

    async def retry_kill(self, reason: str = "operator_retry_kill") -> dict:
        """Run the full kill + verification again after a KILL_FAILED verdict.
        A confirmed kill is final; this never resumes anything."""
        async with self.transition_lock:
            t = self.termination
            if self.state != State.KILL or (t and t.get("verdict") == "KILL_CONFIRMED"):
                return t or {}
            self.ledger.append("kill_retry", {"by": "operator", "previous_checks": (t or {}).get("checks")})
            return await self.interventions.kill(reason)

    async def resume(self, extra_cpu_usec: int = 0) -> dict:
        """Operator resume: unfreeze, optionally extend the CPU budget, then
        de-escalate to the state the CURRENT metrics justify (capped below PAUSE).
        If the budget is still exhausted the next tick will pause again."""
        async with self.transition_lock:
            return await self._resume_locked(extra_cpu_usec)

    async def _resume_locked(self, extra_cpu_usec: int) -> dict:
        if self.state == State.KILL or self._manual_kill:
            return {"resumed": False, "state": self.state.value, "reason": "session terminated (KILL is irreversible)"}
        if self.state != State.PAUSE:
            return {"resumed": False, "state": self.state.value, "reason": "session is not paused"}
        prev = self.state
        self._manual_pause = False
        self._paused_at = None
        ok = await self.interventions.resume(extra_cpu_usec)
        cpu, tokens, mem_peak, wall, cost = self._read()
        target = Thresholds.target_state(cpu, tokens, mem_peak, wall, self._memory_telemetry_valid,
                                         self.budget, cost)
        new_state = target if rank(target) < rank(State.PAUSE) else State.THROTTLE
        self.state = new_state
        self.ledger.append("state_transition", {"from": prev.value, "to": new_state.value,
                                                "cause": "operator_resume",
                                                "utilisation_pct": self._util_rounded()})
        self.ledger.append("manual_resume", {"by": "operator", "extra_cpu_usec": extra_cpu_usec})
        if new_state == State.THROTTLE:
            await self.interventions.throttle()
        return {"resumed": ok, "state": self.state.value,
                "new_cpu_budget_usec": self.budget.cpu_budget_usec}

    async def pre_step_check(self) -> tuple[bool, str]:
        """Called by the orchestrator before each model call and before each
        sandbox exec. The governor decides, not the agent."""
        if self.state == State.KILL or self._manual_kill:
            return False, "terminated"
        async with self.transition_lock:
            if self.state == State.KILL:
                return False, "terminated"
            await self._evaluate_recommendations(source="pre_step")
        if self.orchestrator is not None and getattr(self.orchestrator, "_stop_retries", False):
            return False, "stopped_by_governor:retry_loop"
        return True, ""

    # ---------- tick ----------

    def _read(self):
        cpu = self.cg.cpu_usage_usec()
        mem_peak = self.cg.memory_peak()
        inf = self.orchestrator.inference if self.orchestrator else None
        tokens = inf.committed_tokens() if inf else 0
        cost = inf.committed_cost_usd if inf else 0.0
        return cpu, tokens, mem_peak, self.wall_seconds, cost

    async def _tick(self):
        self.ticks += 1
        cpu_usec = self.cg.cpu_usage_usec()
        mem_current = self.cg.memory_current()
        mem_peak = self.cg.memory_peak()
        inf = self.orchestrator.inference if self.orchestrator else None
        # committed = settled + in-flight + uncertain reservations (not just settled usage)
        tokens = inf.committed_tokens() if inf else await _maybe_await(self.token_spend_fn())
        cost = inf.committed_cost_usd if inf else 0.0
        wall = self.wall_seconds
        now = time.monotonic()
        self._cpu_hist.append((now, cpu_usec))

        # Memory telemetry validation
        if not self._memory_telemetry_valid:
            if self._memory_baseline == 0:
                self._memory_baseline = mem_current
            elif abs(mem_current - self._memory_baseline) > self._memory_validation_delta:
                self._memory_telemetry_valid = True
                self.ledger.append("memory_telemetry_validated", {
                    "baseline_bytes": self._memory_baseline, "current_bytes": mem_current})

        self.last_utilisation = Thresholds.utilisation(
            cpu_usec, tokens, mem_peak, wall, self._memory_telemetry_valid, self.budget, cost)

        if self._manual_kill and self.state != State.KILL:
            await self._transition_to(State.KILL, cpu_usec, tokens, mem_peak, wall, cost)
            self.stop_event.set()
            return

        if self._manual_pause and rank(self.state) < rank(State.PAUSE):
            # Operator pause jumps straight to PAUSE (a human decision, not a metric).
            await self._transition_to(State.PAUSE, cpu_usec, tokens, mem_peak, wall, cost, cause="operator_pause")

        if self.state == State.PAUSE and self._paused_at is not None:
            if now - self._paused_at >= self.pause_timeout_s:
                self.ledger.append("pause_timeout_escalation", {"timeout_s": self.pause_timeout_s})
                self._kill_reason = "pause_timeout"
                await self._transition_to(State.KILL, cpu_usec, tokens, mem_peak, wall, cost)
                self.stop_event.set()
                return

        target = Thresholds.state_for_pct(max(self.last_utilisation.values()), self.budget)
        path = Thresholds.path_to(self.state, target)
        if path:
            nxt = path[0]
            if nxt == State.KILL and not self._kill_reason:
                self._kill_reason = "hard_limit:" + max(self.last_utilisation, key=self.last_utilisation.get)
            await self._transition_to(nxt, cpu_usec, tokens, mem_peak, wall, cost)
            if nxt == State.KILL:
                self.stop_event.set()
                return

        await self._evaluate_recommendations(source="tick")

        if self.runtime is not None and self.ticks % 4 == 1 and hasattr(self.runtime, "metrics"):
            try:
                self._last_runtime_metrics = await asyncio.wait_for(self.runtime.metrics(), timeout=1.0)
            except Exception:
                self._last_runtime_metrics = None

        self.ledger.append("resource_sample", {
            "source": "host_cgroup_v2",
            "cpu_usage_usec": cpu_usec,
            "memory_current_bytes": mem_current,
            "memory_peak_bytes": mem_peak,
            "token_spend": tokens,
            "tokens_settled": inf.total_tokens() if inf else tokens,
            "estimated_cost_usd": round(cost, 6),
            "cost_settled_usd": round(inf.total_cost_usd, 6) if inf else 0.0,
            "wall_seconds": round(wall, 3),
            "memory_telemetry_valid": self._memory_telemetry_valid,
            "utilisation_pct": self._util_rounded(),
            "state": self.state.value,
            "runtime_reported": self._last_runtime_metrics,
        })

    # ---------- recommendations ----------

    def _snapshot(self) -> dict:
        o = self.orchestrator
        cpu_rate = 0.0
        if len(self._cpu_hist) >= 2:
            (t0, c0), (t1, c1) = self._cpu_hist[0], self._cpu_hist[-1]
            if t1 > t0:
                cpu_rate = (c1 - c0) / (t1 - t0)
        return {
            "memory_telemetry_valid": self._memory_telemetry_valid,
            "memory_current": self.cg.memory_peak(),
            "memory_max": self.budget.memory_limit_bytes,
            "tokens_last_step": getattr(o, "tokens_last_step", 0) if o else 0,
            "tokens_median": getattr(o, "tokens_median", 0) if o else 0,
            "context_chars": o.context.total_chars() if o else 0,
            "context_max_chars": o.context.max_chars if o else 0,
            "consecutive_identical_errors": getattr(o, "consecutive_identical_errors", 0) if o else 0,
            "last_error_signature": getattr(o, "last_error_signature", "") if o else "",
            "cpu_rate_usec_per_s": cpu_rate,
            "cpu_remaining_usec": max(0, self.budget.cpu_budget_usec - self.cg.cpu_usage_usec()),
            "agent_running": bool(getattr(o, "running", False)) if o else False,
        }

    async def _evaluate_recommendations(self, source: str):
        recs = self.recommender.evaluate(self._snapshot())
        firing = {r.rule_id for r in recs}
        # re-arm rules whose condition has cleared
        self._armed_rules &= firing
        for rec in recs:
            if rec.rule_id in self._armed_rules:
                continue
            self._armed_rules.add(rec.rule_id)
            d = rec.to_dict()
            d["source"] = source
            d["state_at_issue"] = self.state.value
            d["status"] = "issued"
            self.ledger.append("recommendation_issued", d)
            if self.orchestrator is not None:
                try:
                    await self.orchestrator.receive_recommendation(d)
                except Exception as e:
                    # never silent: a recommendation the agent did not receive is evidence too
                    d["delivery"] = "failed"
                    self.ledger.append("recommendation_delivery_failed", {
                        "rule_id": d.get("rule_id"), "error": f"{type(e).__name__}: {e}"[:300]})
            if rec.auto_applicable:
                d["status"] = "applied" if await self.interventions.apply_recommendation(d) else "failed"
            else:
                d["status"] = "pending_operator"
            self.recommendations.append(d)

    # ---------- transition ----------

    async def _transition_to(self, new_state, cpu_usec, tokens, mem_peak, wall, cost=0.0, cause="metrics"):
        prev = self.state
        self.state = new_state
        self.ledger.append("state_transition", {
            "from": prev.value,
            "to": new_state.value,
            "cause": cause if new_state != State.KILL else (self._kill_reason or cause),
            "cpu_usage_usec": cpu_usec,
            "token_spend": tokens,
            "memory_peak_bytes": mem_peak,
            "wall_seconds": round(wall, 3),
            "estimated_cost_usd": round(cost, 6),
            "utilisation_pct": self._util_rounded(),
        })
        log.info("[%s] %s -> %s", self.session_id, prev.value, new_state.value)

        if new_state == State.RECOMMEND:
            await self._evaluate_recommendations(source="recommend_stage")
        elif new_state == State.THROTTLE:
            await self.interventions.throttle()
        elif new_state == State.PAUSE:
            self._paused_at = time.monotonic()
            await self.interventions.pause()
        elif new_state == State.KILL:
            result = await self.interventions.kill(reason=self._kill_reason or cause)
            if result.get("verdict") != KillVerdict.KILL_CONFIRMED.value:
                log.error("[%s] KILL NOT CONFIRMED: %s", self.session_id, result.get("checks"))

    # ---------- helpers ----------

    def _util_rounded(self) -> dict:
        return {k: round(v, 1) for k, v in self.last_utilisation.items()}

    def _budget_dict(self):
        if hasattr(self.budget, "to_dict"):
            return self.budget.to_dict()
        return {k: getattr(self.budget, k) for k in (
            "cpu_budget_usec", "memory_limit_bytes", "wall_clock_limit_seconds", "token_limit",
            "alert_pct", "recommend_pct", "throttle_pct", "pause_pct")}


async def _maybe_await(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


async def _zero():
    return 0
