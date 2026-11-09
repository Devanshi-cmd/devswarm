"""Tool definitions, the per-agent registry, and error handling."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from anthropic.types import ToolParam
from pydantic import BaseModel, ConfigDict, ValidationError

from devswarm.sandbox.base import Sandbox, SandboxError
from devswarm.tools.paths import Workspace


class ToolError(Exception):
    """A failure the agent should read and react to, such as a non-unique edit match."""


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ToolContext:
    workspace: Workspace
    sandbox: Sandbox


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[ToolInput]
    handler: Callable[[ToolContext, Any], str]

    def definition(self) -> ToolParam:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def only(self, names: Iterable[str]) -> ToolRegistry:
        """A registry limited to one agent's allowlist. Unknown names are a bug, so raise."""
        return ToolRegistry(self._tools[name] for name in names)

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def definitions(self) -> list[ToolParam]:
        # A fixed order matters: tools are part of the prompt prefix, so any reordering
        # would silently invalidate the prompt cache.
        return [self._tools[name].definition() for name in self.names]

    def execute(self, context: ToolContext, name: str, raw_input: dict[str, Any]) -> ToolResult:
        """Run a tool. Expected failures become error results the model can correct."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"tool {name!r} is not available to this agent", is_error=True)
        try:
            parsed = tool.input_model.model_validate(raw_input)
            return ToolResult(tool.handler(context, parsed))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            return ToolResult(f"invalid input: {problems}", is_error=True)
        except (ToolError, SandboxError) as exc:
            return ToolResult(str(exc), is_error=True)
