"""
Trusted inference adapter — the ONLY component that holds the Vultr
Serverless Inference key and the only path to the model. The sandbox never
sees the key and has no network.

Pre-call reservation (enforced before EVERY request, atomically w.r.t. other
calls because the reservation is registered before the first await):

    tokens:  settled + in_flight_reserved + uncertain_reserved + next_reserved <= token_limit
    cost:    C_settled + C_in_flight + C_uncertain + C_next <= cost_limit   (if a cap is set)

next_reserved = estimated prompt tokens (conservative chars/token with a
safety margin) + the FULL permitted completion allowance.

With a cost cap, a model without a verified price is refused (cost cannot be
enforced). Without a cap the call may proceed, and its cost is shown as
UNKNOWN rather than zero.

Revocation (governor KILL): no NEW request is ever sent afterwards. A request
already in flight runs in its own task, shielded from agent cancellation: if
the provider answers, its real usage is recorded and the result is discarded
(never returned to the agent); if no usage arrives, its reservation stays as
UNCERTAIN spend — it is never silently released. A provider 4xx rejection
releases the reservation (nothing was generated).
"""

import asyncio
import itertools
import logging
import math
import time

import httpx

from backend.config import settings
from backend.orchestrator import model_registry
from backend.orchestrator.inference_pricing import cost_usd, has_price, price_for

log = logging.getLogger("agentlimit.inference")

MIN_COMPLETION = 128


class BudgetExceeded(Exception):
    pass


class InferenceRevoked(Exception):
    pass


def estimate_prompt_tokens(messages: list[dict]) -> int:
    """Conservative heuristic (no model tokenizer is available for Vultr-hosted
    models): chars / CHARS_PER_TOKEN, times a safety margin, plus per-message
    overhead. Real tokenisers average ~3.5-4 chars/token for English and code."""
    chars = sum(len(m.get("content", "")) for m in messages)
    return math.ceil(chars / settings.token_estimate_chars_per_token * settings.token_estimate_margin) \
        + 8 * len(messages)


class InferenceAdapter:
    def __init__(self, token_limit: int | None = None, cost_limit_usd: float = 0.0, transport=None):
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0          # settled, priced calls only
        self.unpriced_calls = 0            # settled calls whose model has no verified price
        self.calls = 0
        self.refused_calls = 0
        self.last_model = settings.vultr_model_default
        self.token_limit = token_limit or settings.default_token_budget
        self.cost_limit_usd = cost_limit_usd or 0.0
        self.revoked = False
        self.revoke_reason = ""
        self._throttled = False
        self._transport = transport
        self.on_call = None                # callback(record) -> ledger, installed by the orchestrator
        self._ids = itertools.count(1)
        self.inflight: dict[int, dict] = {}
        self.uncertain: list[dict] = []
        self.settled_after_revoke: list[dict] = []
        self.rejected: list[dict] = []
        self._tasks: set[asyncio.Task] = set()

    # ---------- governor controls ----------

    def set_throttle(self, on: bool) -> None:
        self._throttled = on

    def revoke(self, reason: str = "") -> None:
        self.revoked = True
        self.revoke_reason = reason

    @property
    def cheap_model(self) -> str | None:
        return model_registry.downgrade_model()

    @property
    def downgrade_available(self) -> bool:
        return self.cheap_model is not None

    def _effective_model(self, requested: str) -> str:
        return self.cheap_model if (self._throttled and self.cheap_model) else requested

    # ---------- reservation ----------

    def reserved_tokens(self) -> int:
        return sum(r["reserved_tokens"] for r in self.inflight.values()) + \
            sum(r["reserved_tokens"] for r in self.uncertain)

    def inflight_reserved_cost_usd(self) -> float:
        return sum(r.get("reserved_cost_usd") or 0.0 for r in self.inflight.values())

    def uncertain_reserved_cost_usd(self) -> float:
        return sum(r.get("reserved_cost_usd") or 0.0 for r in self.uncertain)

    def reserved_cost_usd(self) -> float:
        return self.inflight_reserved_cost_usd() + self.uncertain_reserved_cost_usd()

    @property
    def committed_cost_usd(self) -> float:
        """Settled estimate + in-flight + uncertain reservations."""
        return self.total_cost_usd + self.reserved_cost_usd()

    def committed_tokens(self) -> int:
        return self.total_tokens() + self.reserved_tokens()

    def reserve(self, model: str, messages: list[dict], max_tokens: int) -> tuple[int, int, float | None]:
        """Return (allowed_completion, est_prompt, reserved_cost) or raise BudgetExceeded."""
        committed = self.committed_tokens()
        est_in = estimate_prompt_tokens(messages)
        remaining = self.token_limit - committed - est_in
        allowed = min(max_tokens, remaining)
        if allowed < MIN_COMPLETION:
            self.refused_calls += 1
            raise BudgetExceeded(
                f"token reservation refused: committed={committed} est_prompt={est_in} "
                f"limit={self.token_limit} (would leave {remaining} < {MIN_COMPLETION})")
        reserved_cost = None
        if has_price(model):
            p = price_for(model)
            reserved_cost = est_in / 1e6 * p["input_per_million"] + allowed / 1e6 * p["output_per_million"]
        if self.cost_limit_usd > 0:
            if reserved_cost is None:
                self.refused_calls += 1
                raise BudgetExceeded(f"no verified pricing for {model!r}: the dollar cap cannot be enforced")
            projected = self.committed_cost_usd + reserved_cost
            if projected > self.cost_limit_usd:
                self.refused_calls += 1
                raise BudgetExceeded(f"estimated-cost reservation refused: projected ${projected:.5f} "
                                     f"> cap ${self.cost_limit_usd:.5f}")
        return allowed, est_in, reserved_cost

    # ---------- chat ----------

    async def chat(self, model: str, messages: list[dict], max_tokens: int = 1024) -> dict:
        if self.revoked:
            raise InferenceRevoked(f"inference revoked by governor: {self.revoke_reason}")
        effective_model = self._effective_model(model)
        allowed, est_in, reserved_cost = self.reserve(effective_model, messages, max_tokens)
        rid = next(self._ids)
        rec = {"request_id": rid, "requested_model": model, "effective_model": effective_model,
               "throttled": self._throttled,
               "reason": "governance_state_throttle" if effective_model != model else "default",
               "started": time.time(), "reserved_tokens": est_in + allowed, "reserved_prompt_tokens": est_in,
               "max_tokens": allowed, "reserved_cost_usd": reserved_cost}
        self.inflight[rid] = rec            # registered before any await -> atomic reservation

        payload = {"model": effective_model, "messages": messages, "max_tokens": allowed, "temperature": 0.2}
        task = asyncio.create_task(self._send(payload))
        self._tasks.add(task)
        task.add_done_callback(lambda t, r=rec: self._settle(r, t))
        body = await asyncio.shield(task)
        if self.revoked:
            raise InferenceRevoked("inference revoked while the request was in flight; result discarded")
        return body

    async def _send(self, payload: dict) -> dict:
        url = f"{settings.vultr_inference_base}/chat/completions"
        headers = {"Authorization": f"Bearer {settings.vultr_inference_key}"}
        async with httpx.AsyncClient(timeout=settings.inference_timeout_s, transport=self._transport) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            return r.json()

    def _settle(self, rec: dict, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        self.inflight.pop(rec["request_id"], None)
        exc = None if task.cancelled() else task.exception()
        body = task.result() if (not task.cancelled() and exc is None) else None
        usage = (body or {}).get("usage") or {}
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
            rec["outcome"] = f"rejected_by_provider:{exc.response.status_code}"
            self.rejected.append(rec)
        elif body is None or "prompt_tokens" not in usage:
            rec["outcome"] = ("no_usage_in_response" if body is not None
                              else "cancelled" if task.cancelled() else f"request_failed:{type(exc).__name__}")
            rec["revoked_at_settle"] = self.revoked
            self.uncertain.append(rec)
        else:
            in_tok = int(usage.get("prompt_tokens", 0) or 0)
            out_tok = int(usage.get("completion_tokens", 0) or 0)
            self.total_input_tokens += in_tok
            self.total_output_tokens += out_tok
            c = cost_usd(rec["effective_model"], in_tok, out_tok)
            if c is None:
                self.unpriced_calls += 1
            else:
                self.total_cost_usd += c
            self.last_model = rec["effective_model"]
            self.calls += 1
            rec.update(outcome="settled", input_tokens=in_tok, output_tokens=out_tok,
                       estimated_cost_usd=c, result_discarded=self.revoked)
            if self.revoked:
                self.settled_after_revoke.append({"request_id": rec["request_id"], "input_tokens": in_tok,
                                                  "output_tokens": out_tok, "result_discarded": True})
        if self.on_call is not None:
            try:
                self.on_call({k: v for k, v in rec.items() if k != "started"})
            except Exception:
                log.exception("on_call hook failed")

    async def drain(self, timeout: float = 15.0) -> dict:
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)
        for t in list(self._tasks):
            if not t.done():
                t.cancel()
        await asyncio.sleep(0)
        return self.inflight_summary()

    def inflight_summary(self) -> dict:
        return {
            "in_flight": len(self.inflight),
            "in_flight_reserved_tokens": sum(r["reserved_tokens"] for r in self.inflight.values()),
            "uncertain_requests": len(self.uncertain),
            "uncertain_reserved_tokens": sum(r["reserved_tokens"] for r in self.uncertain),
            "settled_after_revoke": list(self.settled_after_revoke),
        }

    # ---------- observability ----------

    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    def snapshot(self) -> dict:
        cost_known = self.unpriced_calls == 0 and has_price(self._effective_model(settings.vultr_model_default))
        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens(),
            "token_limit": self.token_limit,
            "estimated_cost_usd": round(self.total_cost_usd, 6),
            "in_flight_reserved_cost_usd": round(self.inflight_reserved_cost_usd(), 6),
            "uncertain_reserved_cost_usd": round(self.uncertain_reserved_cost_usd(), 6),
            "committed_cost_usd": round(self.committed_cost_usd, 6),
            "cost_limit_usd": self.cost_limit_usd,
            "cost_is_estimate": True,
            "cost_known": cost_known,
            "unpriced_calls": self.unpriced_calls,
            "calls": self.calls,
            "refused_calls": self.refused_calls,
            "throttled": self._throttled,
            "model_downgrade_available": self.downgrade_available,
            "cheap_model": self.cheap_model,
            "downgrade_reason": model_registry.STATE.get("downgrade_reason"),
            "effective_model": self._effective_model(settings.vultr_model_default),
            "revoked": self.revoked,
            **self.inflight_summary(),
            "last_model": self.last_model,
        }


async def list_models() -> list[str]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{settings.vultr_inference_base}/models",
                             headers={"Authorization": f"Bearer {settings.vultr_inference_key}"})
        r.raise_for_status()
        data = r.json().get("data", [])
    return [m.get("id", "") for m in data]
