from pathlib import Path
from typing import Any

import pytest
from fakes import FakeSandbox

from devswarm.agents.base import AgentContext, BaseAgent
from devswarm.agents.coder import CoderAgent
from devswarm.agents.planner import PlannerAgent, PlanOutput, format_plan
from devswarm.agents.prompting import SECURITY_NOTICE
from devswarm.agents.reviewer import ReviewerAgent, ReviewOutput
from devswarm.agents.tester import TesterAgent
from devswarm.config import AgentConfig
from devswarm.llm.mock import MockLLMClient, ScriptedTurn, tool_turn
from devswarm.orchestrator.budget import Budget
from devswarm.storage.trace import TraceStore
from devswarm.tools.base import ToolContext
from devswarm.tools.finish import finish_definition
from devswarm.tools.paths import Workspace
from devswarm.tools.registry import default_registry

ALL_AGENTS = [PlannerAgent, CoderAgent, TesterAgent, ReviewerAgent]
WRITE_TOOLS = {"edit_file", "create_file", "run_command"}

PLAN_INPUT = {"steps": ["a"], "files_to_touch": ["b"], "acceptance_criteria": ["c"], "risks": []}
REVIEW_INPUT = {"verdict": "approve", "summary": "s", "feedback": []}


def build(
    agent_class: Any, root: Path, tmp_path: Path, script: list[ScriptedTurn]
) -> tuple[BaseAgent[Any], MockLLMClient, FakeSandbox, TraceStore]:
    llm = MockLLMClient(script)
    sandbox = FakeSandbox()
    store = TraceStore(tmp_path / "t.sqlite3")
    agent = agent_class(
        AgentConfig(),
        llm,
        default_registry(),
        ToolContext(Workspace(root), sandbox),
        store,
        Budget(),
    )
    return agent, llm, sandbox, store


@pytest.mark.parametrize("agent_class", ALL_AGENTS)
def test_every_agent_treats_repo_content_as_untrusted(agent_class: Any) -> None:
    assert SECURITY_NOTICE in agent_class.system_prompt


@pytest.mark.parametrize("agent_class", ALL_AGENTS)
def test_every_output_model_is_strict(agent_class: Any) -> None:
    assert finish_definition(agent_class.output_model)["strict"] is True


@pytest.mark.parametrize("agent_class", ALL_AGENTS)
def test_every_tool_list_exists(agent_class: Any) -> None:
    default_registry().only(agent_class.tool_names)


@pytest.mark.parametrize("agent_class", [PlannerAgent, ReviewerAgent])
def test_read_only_agents_have_no_write_tools(agent_class: Any) -> None:
    assert not WRITE_TOOLS & set(agent_class.tool_names)


@pytest.mark.parametrize(
    ("agent_class", "finish_input"),
    [(PlannerAgent, PLAN_INPUT), (ReviewerAgent, REVIEW_INPUT)],
)
@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("edit_file", {"path": "a.py", "old_str": "x", "new_str": "y"}),
        ("create_file", {"path": "new.py", "content": "x"}),
        ("run_command", {"cmd": "rm -rf /workspace"}),
    ],
)
def test_read_only_agent_cannot_write_even_if_it_tries(
    agent_class: Any,
    finish_input: dict[str, Any],
    tool: str,
    tool_input: dict[str, Any],
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("x\n")
    script = [tool_turn(tool, tool_input), tool_turn("finish", finish_input, tool_id="f")]
    agent, llm, sandbox, store = build(agent_class, root, tmp_path, script)

    result = agent.run("task", AgentContext(run_id=store.start_run("t")))

    assert result.status == "finished"
    tool_result = llm.requests[1].messages[-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "not available" in tool_result["content"]
    assert (root / "a.py").read_text() == "x\n"
    assert not (root / "new.py").exists()
    assert sandbox.calls == []


def test_reviewer_verdict_must_be_a_known_value() -> None:
    schema = ReviewOutput.model_json_schema()
    assert set(schema["properties"]["verdict"]["enum"]) == {"approve", "request_changes"}


def test_reviewer_verdict_outside_the_enum_can_be_corrected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    script = [
        tool_turn("finish", {"verdict": "lgtm", "summary": "s", "feedback": []}),
        tool_turn("finish", REVIEW_INPUT, tool_id="f"),
    ]
    agent, _, _, store = build(ReviewerAgent, root, tmp_path, script)
    assert agent.run("task", AgentContext(run_id=store.start_run("t"))).turns == 2


def test_system_prompts_are_stable_for_caching() -> None:
    for agent_class in ALL_AGENTS:
        assert "{" not in agent_class.system_prompt


def test_format_plan_has_every_part() -> None:
    text = format_plan(
        PlanOutput(steps=["s1"], files_to_touch=["f1"], acceptance_criteria=["a1"], risks=[])
    )
    assert "- s1" in text and "- f1" in text and "- a1" in text
    assert "- (none)" in text
