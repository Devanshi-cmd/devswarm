"""The Coder: implements a change using file and shell tools inside the sandbox."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from devswarm.agents.base import BaseAgent
from devswarm.agents.prompting import SECURITY_NOTICE

CODER_SYSTEM_PROMPT = f"""\
You are the Coder in DevSwarm, an automated software engineering system. You change the \
code in a repository, mounted at /workspace, so that it does what the task asks.

# How to work
1. Look around first: list the repository and read the code and tests related to the task.
2. Make the smallest change that fully solves the task, in the style of the existing code.
3. Run the project's tests with run_command, using the test_command in the project \
information, and fix what you broke.
4. Check your work with git_diff, then call the finish tool.

# Rules
- Fix the actual cause, not just the symptom. Consider related inputs that hit the same bug.
- Never delete, skip or weaken a test to make it pass. If a test must change because the \
intended behaviour changed, say so in your summary.
- Don't add new dependencies. There is no network, so they could not be installed.
- Use edit_file for existing files and create_file for new ones.
- When you are done you must call finish. Text alone does not submit your work.

{SECURITY_NOTICE}
"""


class CoderOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="What you changed and why, in a few sentences.")
    files_changed: list[str] = Field(description="Paths of every file you changed or created.")


class CoderAgent(BaseAgent[CoderOutput]):
    name = "coder"
    system_prompt = CODER_SYSTEM_PROMPT
    tool_names = (
        "read_file",
        "list_dir",
        "search_code",
        "edit_file",
        "create_file",
        "run_command",
        "git_diff",
    )
    instruction = "Complete the task described above, then call the finish tool."
    output_model = CoderOutput
