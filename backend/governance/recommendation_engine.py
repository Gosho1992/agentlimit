"""
Deterministic recommendation rules.

The engine reads ONLY governor-side observations (cgroup samples, inference
adapter accounting, sandbox exit codes). It never asks the agent to explain
itself. Every recommendation carries the evidence that triggered it.

Rules:
  A memory_pressure   host-side memory peak > 80% of the limit (validated telemetry only)
  B context_growth    last step's token DELTA > 1.5x median of earlier step deltas,
                      or retained prompt context near its cap
  C retry_loop        same error signature >= 3 consecutive times
  D burn_rate         at the current CPU burn rate the session budget runs out
                      within 30 s while the agent is still working
"""

from dataclasses import asdict, dataclass, field
import itertools

_ids = itertools.count(1)


@dataclass
class Recommendation:
    rule_id: str
    severity: str
    metric: str
    observed: float
    threshold: float
    unit: str
    suggestion: str
    action: str
    auto_applicable: bool
    risk: str = ""
    evidence: dict = field(default_factory=dict)
    recommendation_id: str = ""

    def __post_init__(self):
        if not self.recommendation_id:
            self.recommendation_id = f"rec_{next(_ids):03d}"

    def to_dict(self):
        return asdict(self)


class RecommendationEngine:
    RETRY_THRESHOLD = 3
    BURN_HORIZON_S = 30.0

    def evaluate(self, snap: dict) -> list[Recommendation]:
        recs: list[Recommendation] = []

        # Rule A — memory pressure (only once host memory telemetry is validated)
        if snap.get("memory_telemetry_valid") and snap.get("memory_max"):
            limit = 0.8 * snap["memory_max"]
            cur = snap.get("memory_current", 0)
            if cur > limit:
                recs.append(Recommendation(
                    rule_id="memory_pressure",
                    severity="warning",
                    metric="memory.peak",
                    observed=round(cur / 2**20, 1),
                    threshold=round(limit / 2**20, 1),
                    unit="MB",
                    suggestion="Process the data in chunks / stream rows instead of loading everything; "
                               "governor sets memory.high to apply reclaim pressure before the hard cap.",
                    action="set_memory_high",
                    auto_applicable=True,
                    risk="memory.high slows the workload under pressure; it does not lower total work.",
                    evidence={"memory_peak_bytes": cur, "memory_max_bytes": snap["memory_max"]},
                ))

        # Rule B — context / per-step token growth (per-step DELTAS, not cumulative)
        last = snap.get("tokens_last_step", 0) or 0
        median = snap.get("tokens_median", 0) or 0
        ctx_chars = snap.get("context_chars", 0) or 0
        ctx_cap = snap.get("context_max_chars", 0) or 0
        if (median > 0 and last > 1.5 * median) or (ctx_cap and ctx_chars > 0.75 * ctx_cap):
            recs.append(Recommendation(
                rule_id="context_growth",
                severity="warning",
                metric="tokens_per_step",
                observed=last,
                threshold=round(1.5 * median, 1),
                unit="tokens",
                suggestion="Summarise older tool output and compact the retained context before the next model call.",
                action="compact_context",
                auto_applicable=True,
                risk="Compaction drops older error detail the model might have used.",
                evidence={"tokens_last_step": last, "tokens_median": median,
                          "context_chars": ctx_chars, "context_max_chars": ctx_cap},
            ))

        # Rule C — retry loop
        errs = snap.get("consecutive_identical_errors", 0) or 0
        if errs >= self.RETRY_THRESHOLD:
            recs.append(Recommendation(
                rule_id="retry_loop",
                severity="critical",
                metric="consecutive_identical_errors",
                observed=errs,
                threshold=self.RETRY_THRESHOLD,
                unit="errors",
                suggestion="The same error repeated; retrying the same approach is unlikely to work. "
                           "Governor blocks further identical retries and requests human review.",
                action="stop_identical_retries",
                auto_applicable=True,
                risk="Stops the run; a human must decide the next strategy.",
                evidence={"error_signature": snap.get("last_error_signature", "")[:200]},
            ))

        # Rule D — CPU burn-rate projection
        rate = snap.get("cpu_rate_usec_per_s", 0.0) or 0.0
        remaining = snap.get("cpu_remaining_usec", 0) or 0
        if rate > 0 and remaining > 0 and snap.get("agent_running", True):
            eta = remaining / rate
            if eta < self.BURN_HORIZON_S:
                recs.append(Recommendation(
                    rule_id="burn_rate",
                    severity="warning",
                    metric="cpu_seconds_to_exhaustion",
                    observed=round(eta, 1),
                    threshold=self.BURN_HORIZON_S,
                    unit="s",
                    suggestion="At the current CPU burn rate the session budget is exhausted in "
                               f"~{eta:.0f}s. Use a cheaper algorithm, or approve a budget extension.",
                    action="notify_operator",
                    auto_applicable=False,
                    risk="Projection assumes the current rate continues.",
                    evidence={"cpu_rate_usec_per_s": round(rate), "cpu_remaining_usec": remaining},
                ))

        return recs
