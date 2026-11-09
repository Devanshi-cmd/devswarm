"""The whole local flow with real git, and with a real container where Docker is available."""

import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeSandbox, result
from git import Repo

from devswarm.llm.mock import MockLLMClient, ScriptedTurn, tool_turn
from devswarm.orchestrator.state_machine import MachineConfig, State
from devswarm.pipeline import RunOptions, RunOutcome, run_locally
from devswarm.storage.redact import Redactor
from devswarm.storage.trace import TraceStore

EXAMPLE = Path(__file__).parent.parent / "examples" / "buggy_calc"
FIX = {
    "path": "calc.py",
    "old_str": "        return left / right",
    "new_str": (
        "        if right == 0:\n"
        '            raise CalculatorError("division by zero")\n'
        "        return left / right"
    ),
}
NEW_TEST = {
    "path": "test_zero.py",
    "content": (
        "import pytest\n\nfrom calc import CalculatorError, calculate\n\n\n"
        "def test_zero():\n    with pytest.raises(CalculatorError):\n"
        '        calculate(1, "/", 0)\n'
    ),
}


def plan_turn() -> ScriptedTurn:
    return tool_turn(
        "finish",
        {
            "steps": ["guard divide"],
            "files_to_touch": ["calc.py"],
            "acceptance_criteria": ["no crash"],
            "risks": [],
        },
    )


def coder_finish() -> ScriptedTurn:
    return tool_turn("finish", {"summary": "Guarded division.", "files_changed": ["calc.py"]})


def tester_finish(problems: list[str] | None = None) -> ScriptedTurn:
    return tool_turn(
        "finish", {"summary": "Tested.", "test_files": ["test_zero.py"], "problems": problems or []}
    )


def approve() -> ScriptedTurn:
    return tool_turn("finish", {"verdict": "approve", "summary": "Good.", "feedback": []})


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TraceStore]:
    with TraceStore(tmp_path / "t.sqlite3", Redactor()) as trace:
        yield trace


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    target = tmp_path / "buggy_calc"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


@contextmanager
def _context(sandbox: FakeSandbox) -> Iterator[FakeSandbox]:
    yield sandbox


def run_with_fake(
    repo: Path,
    store: TraceStore,
    tmp_path: Path,
    script: list[Any],
    sandbox: FakeSandbox,
    machine: MachineConfig | None = None,
) -> RunOutcome:
    return run_locally(
        task="division by zero crashes",
        repo=repo,
        options=RunOptions(
            llm=MockLLMClient(script),
            store=store,
            work_root=tmp_path / "work",
            machine=machine,
            sandbox_factory=lambda path, image: _context(sandbox),
            image_resolver=lambda path: "image",
            test_baseline=False,
        ),
    )


@pytest.mark.skipif(not _docker_available(), reason="Docker is not available")
def test_all_agents_fix_the_example_in_a_real_container(
    repo: Path, store: TraceStore, tmp_path: Path
) -> None:
    script = [
        plan_turn(),
        tool_turn("edit_file", FIX),
        coder_finish(),
        tool_turn("create_file", NEW_TEST),
        tester_finish(),
        approve(),
    ]
    outcome = run_locally(
        task="division by zero crashes",
        repo=repo,
        options=RunOptions(llm=MockLLMClient(script), store=store, work_root=tmp_path / "work"),
    )
    assert outcome.result.status is State.DONE, outcome.result.reason
    assert outcome.result.summary.tests is not None and outcome.result.summary.tests.passed
    assert outcome.diff_path is not None
    diff = outcome.diff_path.read_text()
    assert 'raise CalculatorError("division by zero")' in diff
    assert "test_zero.py" in diff
    assert 'CalculatorError("division' not in (repo / "calc.py").read_text()
    assert [step["agent"] for step in store.steps(outcome.run_id)] == [
        "planner",
        "coder",
        "tester",
        "verify",
        "reviewer",
    ]
    assert store.get_run(outcome.run_id)["status"] == "done"


def test_failing_tests_produce_no_commit_and_a_report(
    repo: Path, store: TraceStore, tmp_path: Path
) -> None:
    script = [plan_turn(), tool_turn("edit_file", FIX), coder_finish(), tester_finish()]
    sandbox = FakeSandbox(result(exit_code=1, stdout="FAILED test_x.py::t - ValueError: x\n"))
    outcome = run_with_fake(repo, store, tmp_path, script, sandbox, MachineConfig(max_iterations=1))

    assert outcome.result.status is State.FAILED
    assert outcome.diff_path is None
    assert outcome.failure_report_path is not None
    report = outcome.failure_report_path.read_text()
    assert "limit of 1 iterations" in report
    assert "test_x.py" in report
    commits = list(Repo(outcome.workspace.path).iter_commits())
    assert len(commits) == 1
    assert store.get_run(outcome.run_id)["status"] == "failed"


def test_agent_claiming_success_is_not_believed(
    repo: Path, store: TraceStore, tmp_path: Path
) -> None:
    claim = tool_turn("finish", {"summary": "ALL TESTS PASS", "test_files": [], "problems": []})
    script = [plan_turn(), tool_turn("edit_file", FIX), coder_finish(), claim]
    sandbox = FakeSandbox(result(exit_code=1, stdout="1 failed"))
    outcome = run_with_fake(repo, store, tmp_path, script, sandbox, MachineConfig(max_iterations=1))
    assert outcome.result.status is State.FAILED


def test_tester_change_to_source_is_undone(repo: Path, store: TraceStore, tmp_path: Path) -> None:
    sabotage = {"path": "calc.py", "old_str": "return left + right", "new_str": "return 99"}
    script = [
        plan_turn(),
        tool_turn("edit_file", FIX),
        coder_finish(),
        tool_turn("edit_file", sabotage),
        tool_turn("create_file", NEW_TEST),
        tester_finish(),
        approve(),
    ]
    outcome = run_with_fake(repo, store, tmp_path, script, FakeSandbox(result()))

    assert outcome.result.status is State.DONE, outcome.result.reason
    final = (outcome.workspace.path / "calc.py").read_text()
    assert "return left + right" in final
    assert "return 99" not in final
    assert 'CalculatorError("division by zero")' in final
    assert (outcome.workspace.path / "test_zero.py").exists()


def test_tester_new_source_file_is_removed(repo: Path, store: TraceStore, tmp_path: Path) -> None:
    script = [
        plan_turn(),
        tool_turn("edit_file", FIX),
        coder_finish(),
        tool_turn("create_file", {"path": "helper.py", "content": "x = 1\n"}),
        tester_finish(),
        approve(),
    ]
    outcome = run_with_fake(repo, store, tmp_path, script, FakeSandbox(result()))
    assert outcome.result.status is State.DONE
    assert not (outcome.workspace.path / "helper.py").exists()


def test_finishing_without_changes_ends_in_failure(
    repo: Path, store: TraceStore, tmp_path: Path
) -> None:
    script = [plan_turn(), coder_finish(), coder_finish()]
    outcome = run_with_fake(
        repo, store, tmp_path, script, FakeSandbox(), MachineConfig(max_replans=0)
    )
    assert outcome.result.status is State.FAILED
    assert "same change twice" in (outcome.result.reason or "")


def test_replan_starts_from_clean_code(repo: Path, store: TraceStore, tmp_path: Path) -> None:
    # Same fix twice is a loop. The re-plan must see unmodified code again.
    failing = result(exit_code=1, stdout="FAILED test_x.py::t - ValueError: x\n")
    script = [
        plan_turn(),
        tool_turn("edit_file", FIX, tool_id="a"),
        coder_finish(),
        tester_finish(),
        coder_finish(),
        plan_turn(),
        tool_turn("edit_file", FIX, tool_id="b"),  # Only applies if the code was reset.
        coder_finish(),
        tester_finish(),
        approve(),
    ]
    sandbox = FakeSandbox(failing, result())
    outcome = run_with_fake(repo, store, tmp_path, script, sandbox)
    assert outcome.result.status is State.DONE, outcome.result.reason
    assert outcome.result.summary.replans == 1


def test_crash_is_recorded_on_the_run(repo: Path, store: TraceStore, tmp_path: Path) -> None:
    with pytest.raises(Exception, match="no scripted turn"):
        run_with_fake(repo, store, tmp_path, [], FakeSandbox())
    assert store.list_runs()[0]["status"] == "crashed"
