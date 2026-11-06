"""
Trusted inference adapter — the ONLY component that holds the Vultr
Serverless Inference key and the only path to the model. The sandbox never
sees the key and has no network.

Budget enforcement happens BEFORE each call, with a reservation:

    reserved = tokens_used + estimated_prompt_tokens + max_tokens

If the reservation would exceed the token budget, max_tokens is clamped to
what is left; if fewer than MIN_COMPLETION tokens would remain, the call is
refused. So a single response cannot push the session past its token budget
(up to the prompt-size estimate, which is deliberately conservative). The same
reservation is applied to the optional estimated-dollar cap.

revoke() is called by the external governor on KILL. After that, chat()
raises InferenceRevoked before any network I/O: NO NEW request is sent.

Requests already in flight when the governor revokes are handled honestly:
the HTTP request runs in its own task (shielded from agent cancellation), so
if the provider answers we still record its real usage; the result is
discarded and never reaches the agent. Until it settles, its reservation
(estimated prompt + allowed completion) counts against the budget as
UNCERTAIN spend. If no response ever arrives (timeout / connection drop) the
reservation stays recorded as uncertain — we never claim actual usage we did
not receive from the provider.
"""

import asyncio
import itertools
import logging
import time

import httpx

from backend.config import settings
from backend.orchestrator.inference_pricing import cost_usd, price_for

log = logging.getLogger("agentlimit.inference")

MIN_COMPLETION = 128


class BudgetExceeded(Exception):
    pass


class InferenceRevoked(Exception):
    pass


def estimate_prompt_tokens(messages: list[dict]) -> int:
    # ~3 chars/token is conservative for English + code (real tokenisers are ~3.5-4).
    chars = sum(len(m.get("content", "")) for m in messages)
    return chars // 3 + 8 * len(messages)


class InferenceAdapter:
    def __init__(self, token_limit: int | None = None, cost_limit_usd: float = 0.0, transport=None):
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0
        self.calls = 0
        self.refused_calls = 0
        self.last_model = settings.vultr_model_default
        self.token_limit = token_limit or settings.default_token_budget
        self.cost_limit_usd = cost_limit_usd or 0.0
        self.revoked = False
        self.revoke_reason = ""
        self._throttled = False
        self._transport = transport  # injectable for tests
        self.cheap_model = settings.vultr_model_cheap or ""
        # in-flight tracking
        self._ids = itertools.count(1)
        self.inflight: dict[int, dict] = {}
        self.uncertain: list[dict] = []          # reservations with no provider usage
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
    def downgrade_available(self) -> bool:
        return bool(self.cheap_model) and self.cheap_model != settings.vultr_model_default

    def _effective_model(self, requested: str) -> str:
        return self.cheap_model if (self._throttled and self.downgrade_available) else requested

    def reserved_tokens(self) -> int:
        """Tokens reserved by requests in flight or never settled."""
        return sum(r["reserved_tokens"] for r in self.inflight.values()) + \
            sum(r["reserved_tokens"] for r in self.uncertain)

    # ---------- reservation ----------

    def reserve(self, model: str, messages: list[dict], max_tokens: int) -> int:
        """Return the max_tokens the call may use, or raise BudgetExceeded."""
        used = self.total_tokens() + self.reserved_tokens()
        est_in = estimate_prompt_tokens(messages)
        remaining = self.token_limit - used - est_in
        allowed = min(max_tokens, remaining)
        if allowed < MIN_COMPLETION:
            self.refused_calls += 1
            raise BudgetExceeded(
                f"token reservation refused: used={used} est_prompt={est_in} "
                f"limit={self.token_limit} (would leave {remaining} < {MIN_COMPLETION})"
            )
        if self.cost_limit_usd > 0:
            p_in, p_out = price_for(model)
            projected = self.total_cost_usd + self.reserved_cost_usd() + est_in / 1e6 * p_in + allowed / 1e6 * p_out
            if projected > self.cost_limit_usd:
                self.refused_calls += 1
                raise BudgetExceeded(
                    f"estimated-cost reservation refused: projected ${projected:.5f} > cap ${self.cost_limit_usd:.5f}"
                )
        return allowed

    def reserved_cost_usd(self) -> float:
        rs = list(self.inflight.values()) + self.uncertain
        return sum(r.get("reserved_cost_usd", 0.0) for r in rs)

    # ---------- chat ----------

    async def chat(self, model: str, messages: list[dict], max_tokens: int = 1024) -> dict:
        if self.revoked:
            raise InferenceRevoked(f"inference revoked by governor: {self.revoke_reason}")
        effective_model = self._effective_model(model)
        allowed = self.reserve(effective_model, messages, max_tokens)
        est_in = estimate_prompt_tokens(messages)
        p_in, p_out = price_for(effective_model)
        rid = next(self._ids)
        rec = {"request_id": rid, "model": effective_model, "started": time.time(),
               "reserved_tokens": est_in + allowed,
               "reserved_cost_usd": est_in / 1e6 * p_in + allowed / 1e6 * p_out}
        self.inflight[rid] = rec

        payload = {"model": effective_model, "messages": messages, "max_tokens": allowed, "temperature": 0.2}
        task = asyncio.create_task(self._send(payload))
        self._tasks.add(task)
        task.add_done_callback(lambda t, r=rec: self._settle(r, t))
        # shield: cancelling the agent must not abort the HTTP request whose
        # usage we still need to record.
        body = await asyncio.shield(task)
        if self.revoked:
            raise InferenceRevoked("inference revoked by governor while request was in flight; result discarded")
        return body

    async def _send(self, payload: dict) -> dict:
        url = f"{settings.vultr_inference_base}/chat/completions"
        headers = {"Authorization": f"Bearer {settings.vultr_inference_key}"}
        async with httpx.AsyncClient(timeout=90, transport=self._transport) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            return r.json()

    def _settle(self, rec: dict, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        self.inflight.pop(rec["request_id"], None)
        body = None
        if not task.cancelled() and task.exception() is None:
            body = task.result()
        usage = (body or {}).get("usage") or {}
        exc = None if task.cancelled() else task.exception()
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
            # Provider rejected the request (4xx): nothing was generated; release the reservation.
            rec["outcome"] = f"rejected_by_provider:{exc.response.status_code}"
            self.rejected.append(rec)
            return
        if body is None or "prompt_tokens" not in usage:
            # Provider usage unknown: keep the reservation as uncertain spend.
            rec["outcome"] = "no_usage_received" if body is not None else \
                f"request_failed:{type(task.exception()).__name__}" if not task.cancelled() and task.exception() else "cancelled"
            rec["revoked_at_settle"] = self.revoked
            self.uncertain.append(rec)
            return
        in_tok = int(usage.get("prompt_tokens", 0) or 0)
        out_tok = int(usage.get("completion_tokens", 0) or 0)
        self.total_input_tokens += in_tok
        self.total_output_tokens += out_tok
        self.total_cost_usd += cost_usd(rec["model"], in_tok, out_tok)
        self.last_model = rec["model"]
        self.calls += 1
        if self.revoked:
            self.settled_after_revoke.append({"request_id": rec["request_id"], "input_tokens": in_tok,
                                              "output_tokens": out_tok, "result_discarded": True})

    async def drain(self, timeout: float = 15.0) -> dict:
        """Wait for in-flight requests to settle (used at kill / session close)."""
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
        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens(),
            "token_limit": self.token_limit,
            "estimated_cost_usd": round(self.total_cost_usd, 6),
            "cost_usd": round(self.total_cost_usd, 6),
            "cost_limit_usd": self.cost_limit_usd,
            "cost_is_estimate": True,
            "calls": self.calls,
            "refused_calls": self.refused_calls,
            "throttled": self._throttled,
            "model_downgrade_available": self.downgrade_available,
            "cheap_model": self.cheap_model if self.downgrade_available else None,
            "revoked": self.revoked,
            **self.inflight_summary(),
            "last_model": self.last_model,
        }


async def list_models() -> list[str]:
    """Live model IDs from Vultr Serverless Inference (used at startup to
    fail loudly if the configured model IDs do not exist)."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{settings.vultr_inference_base}/models",
                             headers={"Authorization": f"Bearer {settings.vultr_inference_key}"})
        r.raise_for_status()
        data = r.json().get("data", [])
    return [m.get("id", "") for m in data]
