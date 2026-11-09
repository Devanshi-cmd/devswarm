"""The interface between DevSwarm and any LLM client, real or mocked."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from anthropic.types import ContentBlock, MessageParam, ToolParam
from pydantic import BaseModel

from devswarm.llm.pricing import TokenUsage

Effort = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class LLMRequest:
    model: str
    system: str
    messages: Sequence[MessageParam]
    tools: Sequence[ToolParam] = ()
    max_tokens: int = 16_000
    effort: Effort = "medium"


@dataclass(frozen=True)
class RetryRecord:
    attempt: int
    reason: str
    delay_seconds: float


@dataclass(frozen=True)
class LLMResponse:
    id: str
    model: str
    content: list[ContentBlock]
    stop_reason: str
    usage: TokenUsage
    cost_usd: float
    latency_ms: int
    refusal_category: str | None = None
    retries: tuple[RetryRecord, ...] = ()


class LLMError(Exception):
    """An LLM call failed for good: either not retryable, or out of retries."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retries: tuple[RetryRecord, ...] = (),
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retries = retries


class LLMClient(Protocol):
    def create(self, request: LLMRequest) -> LLMResponse: ...


def to_jsonable(value: Any) -> Any:
    """Convert messages, which may hold SDK objects, into plain JSON-compatible data."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(item) for item in value]
    return value
