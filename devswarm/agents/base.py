"""BaseAgent: the Messages API tool-use loop, written out in full."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from anthropic.types import (
    ContentBlockParam,
    MessageParam,
    TextBlockParam,
    ToolResultBlockParam,
    ToolUseBlock,
)
from pydantic import BaseModel

from devswarm.agents.prompting import Section, render_user_message
from devswarm.config import AgentConfig
from devswarm.llm.types import LLMClient, LLMError, LLMRequest
from devswarm.orchestrator.budget import Budget
from devswarm.storage.trace import TraceStore
from devswarm.tools.base import ToolContext, ToolRegistry, ToolResult
from devswarm.tools.finish import FINISH_TOOL_NAME, finish_definition, parse_finish

AgentStatus = Literal[
    "finished", "max_turns", "budget_exhausted", "context_exhausted", "refused", "error"
]

# Kept below Haiku's 100K pricing threshold. History is append-only, so the agent
# cannot shrink its context; it can only be told to finish before reaching the limit.
CONTEXT_WRAP_UP_TOKENS = 85_000
CONTEXT_LIMIT_TOKENS = 95_000

_MAX_FINISH_NUDGES = 2
_TURNS_LEFT_WARNING = 2
_EVENT_INPUT_CHARS = 100

_WRAP_UP_REMINDER = "Your context is nearly full. Finish now by calling the finish tool."
_NUDGE = "You stopped without submitting a result. Call the finish tool now."


@dataclass(frozen=True)
class AgentEvent:
    agent: str
    kind: Literal["turn", "tool", "finish"]
    detail: str
    is_error: bool = False


EventHandler = Callable[[AgentEvent], None]


@dataclass(frozen=True)
class AgentContext:
    run_id: str
    iteration: int = 0
    sections: Sequence[Section] = ()


@dataclass(frozen=True)
class AgentResult[OutputT: BaseModel]:
    status: AgentStatus
    step_id: int
    turns: int
    output: OutputT | None = None
    error: str | None = None


class BaseAgent[OutputT: BaseModel]:
    name: ClassVar[str]
    system_prompt: ClassVar[str]
    tool_names: ClassVar[tuple[str, ...]]
    instruction: ClassVar[str]
    output_model: type[OutputT]

    def __init__(
        self,
        config: AgentConfig,
        llm: LLMClient,
        tools: ToolRegistry,
        tool_context: ToolContext,
        store: TraceStore,
        budget: Budget,
        on_event: EventHandler | None = None,
    ) -> None:
        self._config = config
        self._llm = llm
        self._tools = tools.only(self.tool_names)
        self._tool_context = tool_context
        self._store = store
        self._budget = budget
        self._on_event = on_event or (lambda event: None)
        # Built once and kept in a fixed order, because tools are part of the cached prefix.
        self._definitions = [*self._tools.definitions(), finish_definition(self.output_model)]

    def run(self, task: str, context: AgentContext) -> AgentResult[OutputT]:
        step_id = self._store.start_step(context.run_id, self.name, iteration=context.iteration)
        try:
            result = self._loop(step_id, task, context)
        except BaseException:
            self._store.finish_step(step_id, "crashed")
            raise
        self._store.finish_step(step_id, result.status, summary=result.error)
        return result

    def _loop(self, step_id: int, task: str, context: AgentContext) -> AgentResult[OutputT]:
        first_message = render_user_message(
            [Section("task", task), *context.sections], self.instruction
        )
        # Append-only: messages are never edited or removed once sent. The API rejects
        # replayed thinking blocks after a history edit, and edits also break the cache.
        messages: list[MessageParam] = [{"role": "user", "content": first_message}]
        nudges = 0
        wrap_up_sent = False
        max_turns = self._config.max_turns

        for turn in range(1, max_turns + 1):
            # Checked before spending, so a run can overshoot by at most one call.
            if reason := self._budget.exceeded():
                return AgentResult("budget_exhausted", step_id, turn - 1, error=reason)

            self._emit("turn", f"turn {turn}/{max_turns}")
            request = LLMRequest(
                model=self._config.model,
                system=self.system_prompt,
                messages=list(messages),
                tools=self._definitions,
                max_tokens=self._config.max_tokens,
                effort=self._config.effort,
            )
            try:
                response = self._llm.create(request)
            except LLMError as exc:
                self._store.record_llm_error(step_id, request, exc)
                return AgentResult("error", step_id, turn, error=str(exc))
            llm_call_id = self._store.record_llm_call(step_id, request, response)
            self._budget.charge(response.usage, response.cost_usd)

            # Checked first: a refusal has no usable content, and a response cut off by
            # max_tokens may hold an incomplete tool call, which must never run.
            if response.stop_reason == "refusal":
                category = response.refusal_category or "unspecified"
                return AgentResult("refused", step_id, turn, error=f"model declined: {category}")
            if response.stop_reason == "max_tokens":
                return AgentResult("error", step_id, turn, error="response hit max_tokens")
            if response.stop_reason not in ("tool_use", "end_turn"):
                error = f"unexpected stop reason: {response.stop_reason}"
                return AgentResult("error", step_id, turn, error=error)

            if response.content:
                messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                if nudges >= _MAX_FINISH_NUDGES:
                    error = "agent stopped repeatedly without calling finish"
                    return AgentResult("error", step_id, turn, error=error)
                nudges += 1
                messages.append({"role": "user", "content": _NUDGE})
                continue

            tool_uses = [block for block in response.content if isinstance(block, ToolUseBlock)]
            results, output = self._run_tools(step_id, llm_call_id, tool_uses)
            if output is not None:
                return AgentResult("finished", step_id, turn, output=output)

            prompt_tokens = response.usage.prompt_tokens
            if prompt_tokens >= CONTEXT_LIMIT_TOKENS:
                error = f"context reached {prompt_tokens} tokens"
                return AgentResult("context_exhausted", step_id, turn, error=error)

            reply: list[ContentBlockParam] = list(results)
            if prompt_tokens >= CONTEXT_WRAP_UP_TOKENS and not wrap_up_sent:
                wrap_up_sent = True
                reply.append(_text(_WRAP_UP_REMINDER))
            if max_turns - turn == _TURNS_LEFT_WARNING:
                reply.append(_text(f"You have {_TURNS_LEFT_WARNING} turns left. Finish soon."))
            # One user message for all results; splitting them discourages parallel calls.
            messages.append({"role": "user", "content": reply})

        return AgentResult("max_turns", step_id, max_turns, error="ran out of turns")

    def _run_tools(
        self, step_id: int, llm_call_id: int, tool_uses: list[ToolUseBlock]
    ) -> tuple[list[ToolResultBlockParam], OutputT | None]:
        results: list[ToolResultBlockParam] = []
        output: OutputT | None = None
        # finish goes last, so work requested in the same turn still happens first.
        for block in sorted(tool_uses, key=lambda use: use.name == FINISH_TOOL_NAME):
            started = time.perf_counter()
            if block.name == FINISH_TOOL_NAME:
                outcome, output = self._submit(block.input)
            else:
                outcome = self._tools.execute(self._tool_context, block.name, block.input)
            self._store.record_tool_call(
                step_id,
                llm_call_id,
                tool_name=block.name,
                tool_input=block.input,
                output=outcome.content,
                is_error=outcome.is_error,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            kind: Literal["tool", "finish"] = "finish" if block.name == FINISH_TOOL_NAME else "tool"
            self._emit(kind, _describe(block), outcome.is_error)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": outcome.content,
                    "is_error": outcome.is_error,
                }
            )
        return results, output

    def _submit(self, raw_input: dict[str, Any]) -> tuple[ToolResult, OutputT | None]:
        parsed = parse_finish(self.output_model, raw_input)
        if isinstance(parsed, str):
            return ToolResult(parsed, is_error=True), None
        return ToolResult("accepted"), parsed

    def _emit(
        self, kind: Literal["turn", "tool", "finish"], detail: str, error: bool = False
    ) -> None:
        self._on_event(AgentEvent(self.name, kind, detail, error))


def _text(text: str) -> TextBlockParam:
    return {"type": "text", "text": text}


def _describe(block: ToolUseBlock) -> str:
    if block.name == FINISH_TOOL_NAME:
        return "submitted result"
    arguments = json.dumps(block.input)
    if len(arguments) > _EVENT_INPUT_CHARS:
        arguments = arguments[:_EVENT_INPUT_CHARS] + "..."
    return f"{block.name} {arguments}"
