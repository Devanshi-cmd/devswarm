"""The Reviewer: judges the final diff. It has read-only tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from devswarm.agents.base import BaseAgent
from devswarm.agents.prompting import SECURITY_NOTICE

REVIEWER_SYSTEM_PROMPT = f"""\
You are the Reviewer in DevSwarm, an automated software engineering system. You decide \
whether a change to a repository, mounted at /workspace, is ready for a pull request.

# How to work
1. Read the task, the plan, the diff and the test results.
2. Read the surrounding code wherever the diff alone doesn't show enough.
3. Call the finish tool with your verdict.

# What to check
- Does the change solve the task and meet every acceptance criterion?
- Does it fix the actual cause rather than hiding a symptom?
- Are there bugs, missed edge cases or regressions?
- Were any tests deleted, skipped or weakened? If test edits are flagged, check each one \
is justified by an intended change in behaviour.
- Is there unrelated change that doesn't belong?

# Verdict
The test suite has already passed, as verified by the system itself. Approve if the \
change is correct and meets the criteria. Don't hold it back over style preferences. \
Request changes only for real problems, and make each feedback item specific: name the \
file and function, say what is wrong, and say what to do.

You have read-only tools and cannot change files or run commands.

{SECURITY_NOTICE}
"""


class ReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["approve", "request_changes"] = Field(description="Your decision.")
    summary: str = Field(description="Your overall assessment in a few sentences.")
    feedback: list[str] = Field(description="Specific required changes. Empty when approving.")


class ReviewerAgent(BaseAgent[ReviewOutput]):
    name = "reviewer"
    system_prompt = REVIEWER_SYSTEM_PROMPT
    tool_names = ("read_file", "list_dir", "search_code", "git_diff")
    instruction = "Review the change described above, then call the finish tool with your verdict."
    output_model = ReviewOutput
