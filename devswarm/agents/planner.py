"""The Planner: explores the repository and writes a plan. It cannot change anything."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from devswarm.agents.base import BaseAgent
from devswarm.agents.prompting import SECURITY_NOTICE

PLANNER_SYSTEM_PROMPT = f"""\
You are the Planner in DevSwarm, an automated software engineering system. You read a \
task and a repository, mounted at /workspace, and write the plan another agent will follow.

# How to work
1. Find the code involved: list the repository, search it, and read the relevant files.
2. Work out the actual cause of the problem, or where a new feature belongs.
3. Call the finish tool with your plan.

# What a good plan looks like
- steps: a short ordered list of concrete changes that name functions and files.
- files_to_touch: only the files that need changing.
- acceptance_criteria: observable behaviour someone could verify with a test.
- risks: edge cases and behaviour that could regress.
Solve only what the task asks for. Don't plan refactoring or unrelated improvements.

If a previous attempt is included, it failed. Work out why, and plan a different approach.

You have read-only tools and cannot change files or run commands.

{SECURITY_NOTICE}
"""


class PlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[str] = Field(description="Ordered, concrete implementation steps.")
    files_to_touch: list[str] = Field(description="Paths of the files that need changing.")
    acceptance_criteria: list[str] = Field(description="Verifiable conditions for success.")
    risks: list[str] = Field(description="Edge cases and possible regressions.")


class PlannerAgent(BaseAgent[PlanOutput]):
    name = "planner"
    system_prompt = PLANNER_SYSTEM_PROMPT
    tool_names = ("read_file", "list_dir", "search_code")
    instruction = "Explore the repository as needed, then call the finish tool with your plan."
    output_model = PlanOutput


def format_plan(plan: PlanOutput) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) or "- (none)"

    return (
        f"Steps:\n{bullets(plan.steps)}\n\n"
        f"Files to touch:\n{bullets(plan.files_to_touch)}\n\n"
        f"Acceptance criteria:\n{bullets(plan.acceptance_criteria)}\n\n"
        f"Risks:\n{bullets(plan.risks)}"
    )
