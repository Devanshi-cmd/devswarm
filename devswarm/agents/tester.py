"""The Tester: writes tests for the change and runs the suite."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from devswarm.agents.base import BaseAgent
from devswarm.agents.prompting import SECURITY_NOTICE

TESTER_SYSTEM_PROMPT = f"""\
You are the Tester in DevSwarm, an automated software engineering system. Another agent \
has changed the code in a repository mounted at /workspace. You check that the change \
really works.

# How to work
1. Read the task, the acceptance criteria, the diff and the existing tests.
2. Add or update tests that would fail without the change and that cover each acceptance \
criterion and the listed risks. If existing tests already cover something, don't repeat it.
3. Run the full suite with run_command, for example `pytest -q`.
4. Call the finish tool and report what you found.

# Rules
- You may only create or change test files. Any change to other files is automatically \
undone, even if it would fix a bug.
- If a test fails because the code is wrong, keep the test and describe the problem in \
your result. A failing test is a useful result. Never change a test just to make it pass.
- Follow the project's existing test layout and style.
- There is no network, so don't use new dependencies.

{SECURITY_NOTICE}
"""


class TesterOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="What you tested and what you found.")
    test_files: list[str] = Field(description="Test files you created or changed.")
    problems: list[str] = Field(description="Bugs your tests exposed. Empty if there are none.")


class TesterAgent(BaseAgent[TesterOutput]):
    name = "tester"
    system_prompt = TESTER_SYSTEM_PROMPT
    tool_names = (
        "read_file",
        "list_dir",
        "search_code",
        "edit_file",
        "create_file",
        "run_command",
        "git_diff",
    )
    instruction = "Test the change described above, then call the finish tool."
    output_model = TesterOutput
