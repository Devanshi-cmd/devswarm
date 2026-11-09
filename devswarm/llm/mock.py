"""A scripted LLM client, so the whole test suite runs offline and free.

Besides replaying canned turns, it imitates two API behaviours our code depends on:
prompt caching, and the 400 returned when earlier history is edited.
"""

from __future__ import annotations

import copy
import json
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from anthropic.types import ContentBlock, TextBlock, ThinkingBlock, ToolUseBlock

from devswarm.llm.pricing import TokenUsage, calculate_cost, warn_if_near_long_context
from devswarm.llm.types import LLMError, LLMRequest, LLMResponse, to_jsonable

_MIN_CACHEABLE_TOKENS = 512


class MockScriptExhausted(Exception):
    """The code under test made more LLM calls than its script allows."""


@dataclass(frozen=True)
class ScriptedTurn:
    content: list[ContentBlock]
    stop_reason: str = "end_turn"
    refusal_category: str | None = None
    usage: TokenUsage | None = None  # Overrides the simulated usage when set.


def text_turn(text: str, *, usage: TokenUsage | None = None) -> ScriptedTurn:
    return ScriptedTurn(content=[TextBlock(type="text", text=text)], usage=usage)


def tool_turn(name: str, tool_input: dict[str, Any], *, tool_id: str = "toolu_1") -> ScriptedTurn:
    return ScriptedTurn(
        content=[
            ThinkingBlock(type="thinking", thinking="mock reasoning", signature="mock-signature"),
            ToolUseBlock(type="tool_use", id=tool_id, name=name, input=tool_input),
        ],
        stop_reason="tool_use",
    )


def refusal_turn(category: str = "cyber") -> ScriptedTurn:
    return ScriptedTurn(content=[], stop_reason="refusal", refusal_category=category)


def _estimate_tokens(value: Any) -> int:
    return len(json.dumps(value)) // 4 + 1


@dataclass
class _Conversation:
    messages: list[Any]
    prompt_tokens: int


class MockLLMClient:
    def __init__(self, script: Iterable[ScriptedTurn | Exception] = ()) -> None:
        self._script = deque(script)
        self._conversations: dict[str, _Conversation] = {}
        self._cached_prefixes: set[str] = set()
        self.requests: list[LLMRequest] = []

    def create(self, request: LLMRequest) -> LLMResponse:
        # Snapshot it: agents keep appending to the same list after this call returns.
        self.requests.append(replace(request, messages=copy.deepcopy(list(request.messages))))
        if not self._script:
            raise MockScriptExhausted(f"no scripted turn left for call {len(self.requests)}")

        simulated = self._simulate_usage(request)
        item = self._script.popleft()
        if isinstance(item, Exception):
            raise item

        output_tokens = _estimate_tokens(to_jsonable(item.content))
        usage = item.usage or replace(simulated, output_tokens=output_tokens)
        warn_if_near_long_context(request.model, usage)
        return LLMResponse(
            id=f"msg_mock_{len(self.requests)}",
            model=request.model,
            content=item.content,
            stop_reason=item.stop_reason,
            usage=usage,
            cost_usd=calculate_cost(request.model, usage),
            latency_ms=1,
            refusal_category=item.refusal_category,
        )

    def _simulate_usage(self, request: LLMRequest) -> TokenUsage:
        messages: list[Any] = to_jsonable(list(request.messages))
        prefix = [request.model, request.system, to_jsonable(list(request.tools))]
        prefix_key = json.dumps(prefix)
        conversation_key = json.dumps([*prefix, messages[0] if messages else None])

        previous = self._conversations.get(conversation_key)
        if (
            previous is not None
            and len(messages) > len(previous.messages)
            and messages[: len(previous.messages)] != previous.messages
        ):
            raise LLMError("HTTP 400: earlier conversation history was edited", status_code=400)

        total = _estimate_tokens([prefix, messages])
        self._conversations[conversation_key] = _Conversation(messages, total)
        if total < _MIN_CACHEABLE_TOKENS:
            return TokenUsage(input_tokens=total)

        if previous is not None:
            cached = min(previous.prompt_tokens, total)
        elif prefix_key in self._cached_prefixes:
            cached = _estimate_tokens(prefix)
        else:
            cached = 0
        self._cached_prefixes.add(prefix_key)
        return TokenUsage(
            cache_read_input_tokens=cached, cache_creation_input_tokens=total - cached
        )
