"""The real LLM client: one Messages API call, with retries and cost accounting."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

import anthropic
from anthropic import omit
from anthropic.types import Message

from devswarm.llm.pricing import TokenUsage, calculate_cost, warn_if_near_long_context
from devswarm.llm.types import LLMError, LLMRequest, LLMResponse, RetryRecord

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 5
    base_delay: float = 1.0
    max_delay: float = 120.0


class AnthropicLLMClient:
    def __init__(
        self,
        sdk: anthropic.Anthropic,
        *,
        policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        # The SDK can retry by itself. We turn that off so that every retry happens
        # in the loop below, where it is visible and gets recorded in the trace.
        self._sdk = sdk.with_options(max_retries=0, timeout=_REQUEST_TIMEOUT_SECONDS)
        self._policy = policy or RetryPolicy()
        self._sleep = sleep
        self._rng = rng

    @classmethod
    def from_api_key(cls, api_key: str) -> AnthropicLLMClient:
        return cls(anthropic.Anthropic(api_key=api_key))

    def create(self, request: LLMRequest) -> LLMResponse:
        retries: list[RetryRecord] = []
        attempt = 0
        while True:
            attempt += 1
            started = time.perf_counter()
            retry_after: float | None = None
            status_code: int | None = None
            try:
                message = self._send(request)
            except anthropic.APIStatusError as exc:
                status_code = exc.status_code
                if status_code != 429 and status_code < 500:
                    raise LLMError(
                        f"HTTP {status_code}: {exc.message}",
                        status_code=status_code,
                        retries=tuple(retries),
                    ) from exc
                reason = f"HTTP {status_code}"
                retry_after = _parse_retry_after(exc.response.headers.get("retry-after"))
            except anthropic.APIConnectionError as exc:
                reason = type(exc).__name__
            except anthropic.APIError as exc:
                raise LLMError(str(exc), retries=tuple(retries)) from exc
            else:
                latency_ms = int((time.perf_counter() - started) * 1000)
                return _to_response(message, request.model, latency_ms, tuple(retries))

            if attempt > self._policy.max_retries:
                raise LLMError(
                    f"giving up after {attempt} attempts, last error: {reason}",
                    status_code=status_code,
                    retries=tuple(retries),
                )
            delay = self._delay(attempt, retry_after)
            retries.append(RetryRecord(attempt=attempt, reason=reason, delay_seconds=delay))
            logger.warning("LLM call failed (%s); retry %d in %.1fs", reason, attempt, delay)
            self._sleep(delay)

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self._policy.max_delay)
        # "Full jitter": a random wait up to the exponential limit. Without randomness,
        # callers rate-limited together would all retry together and collide again.
        limit: float = min(self._policy.max_delay, self._policy.base_delay * 2 ** (attempt - 1))
        return self._rng() * limit

    def _send(self, request: LLMRequest) -> Message:
        return self._sdk.messages.create(
            model=request.model,
            max_tokens=request.max_tokens,
            # Tools render before system, so this breakpoint caches both.
            system=[
                {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
            ],
            messages=list(request.messages),
            tools=list(request.tools) if request.tools else omit,
            # Thinking text is hidden by default; a summary lets the dashboard show it.
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": request.effort},
            # Moves forward with the conversation, so past turns are read from cache.
            cache_control={"type": "ephemeral"},
        )


def _parse_retry_after(value: str | None) -> float | None:
    try:
        return max(float(value), 0.0) if value is not None else None
    except ValueError:  # Retry-After may also be an HTTP date; fall back to our own backoff.
        return None


def _to_response(
    message: Message, priced_model: str, latency_ms: int, retries: tuple[RetryRecord, ...]
) -> LLMResponse:
    # Priced by the ID we asked for: the API may echo a variant our table doesn't list,
    # and failing here would lose the record of a call that has already been paid for.
    usage = TokenUsage(
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        cache_read_input_tokens=message.usage.cache_read_input_tokens or 0,
        cache_creation_input_tokens=message.usage.cache_creation_input_tokens or 0,
    )
    warn_if_near_long_context(priced_model, usage)
    return LLMResponse(
        id=message.id,
        model=message.model,
        content=message.content,
        stop_reason=message.stop_reason or "unknown",
        usage=usage,
        cost_usd=calculate_cost(priced_model, usage),
        latency_ms=latency_ms,
        refusal_category=message.stop_details.category if message.stop_details else None,
        retries=retries,
    )
