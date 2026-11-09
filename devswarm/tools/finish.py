"""The finish tool: how an agent submits its structured result.

Unlike the other tools it has no handler. Its schema differs per agent, and the agent
loop reads the result directly instead of executing anything.
"""

from __future__ import annotations

from typing import Any

from anthropic.types import ToolParam
from pydantic import BaseModel, ValidationError

FINISH_TOOL_NAME = "finish"


def finish_definition(output_model: type[BaseModel]) -> ToolParam:
    schema = output_model.model_json_schema()
    if schema.get("additionalProperties") is not False:
        raise ValueError(f"{output_model.__name__} must set extra='forbid' for strict tool use")
    return {
        "name": FINISH_TOOL_NAME,
        "description": "Submit your final result. Call this exactly once, when you are done.",
        "input_schema": schema,
        "strict": True,
    }


def parse_finish[OutputT: BaseModel](
    output_model: type[OutputT], raw_input: dict[str, Any]
) -> OutputT | str:
    """Return the validated output, or an error message for the model to correct."""
    try:
        return output_model.model_validate(raw_input)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        return f"invalid result: {problems}"
