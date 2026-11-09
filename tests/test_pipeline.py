"""The whole local flow on a real container, with only the model scripted."""

import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeSandbox, result

from devswarm.llm.mock import MockLLMClient, ScriptedTurn, text_turn, tool_turn
from devswarm.pipeline import run_coder_locally
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
FINISH = {"summary": "Raise CalculatorError on division by zero.", "files_changed": ["calc.py"]}


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def fixing_script() -> list[ScriptedTurn]:
    return [
        tool_turn("read_file", {"path": "calc.py"}, tool_id="t1"),
        tool_turn("edit_file", FIX, tool_id="t2"),
        tool_turn("run_command", {"cmd": "pytest -q", "timeout": 60}, tool_id="t3"),
        tool_turn("finish", FINISH, tool_id="t4"),
    ]


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
def fake_factory_context(sandbox: FakeSandbox) -> Iterator[FakeSandbox]:
    yield sandbox


def run_with_fake(
    repo: Path, store: TraceStore, tmp_path: Path, script: list[Any], sandbox: FakeSandbox
) -> Any:
    return run_coder_locally(
        task="division by zero crashes",
        repo=repo,
        llm=MockLLMClient(script),
        store=store,
        work_root=tmp_path / "work",
        sandbox_factory=lambda path, image: fake_factory_context(sandbox),
        image_resolver=lambda path: "image",
    )


@pytest.mark.skipif(not _docker_available(), reason="Docker is not available")
def test_real_container_fixes_the_example(repo: Path, store: TraceStore, tmp_path: Path) -> None:
    outcome = run_coder_locally(
        task="division by zero crashes",
        repo=repo,
        llm=MockLLMClient(fixing_script()),
        store=store,
        work_root=tmp_path / "work",
    )
    assert outcome.status == "done", outcome.error
    assert outcome.tests_passed
    assert outcome.diff_path is not None
    assert 'raise CalculatorError("division by zero")' in outcome.diff_path.read_text()
    assert "return left / right\n" in (repo / "calc.py").read_text()
    assert 'CalculatorError("division' not in (repo / "calc.py").read_text()

    run = store.get_run(outcome.run_id)
    assert run["status"] == "done"
    assert run["llm_calls"] == 4


def test_failing_tests_do_not_produce_a_commit(
    repo: Path, store: TraceStore, tmp_path: Path
) -> None:
    sandbox = FakeSandbox(result(exit_code=1, stdout="1 failed"))
    finish = tool_turn("finish", FINISH, tool_id="t1")
    outcome = run_with_fake(repo, store, tmp_path, [finish], sandbox)

    assert outcome.status == "failed"
    assert not outcome.tests_passed
    assert "exit code 1" in outcome.error
    assert outcome.diff_path is None


def test_model_claim_of_success_is_not_trusted(
    repo: Path, store: TraceStore, tmp_path: Path
) -> None:
    sandbox = FakeSandbox(result(exit_code=2))
    claim = tool_turn("finish", {"summary": "All tests pass!", "files_changed": []}, tool_id="t1")
    assert run_with_fake(repo, store, tmp_path, [claim], sandbox).status == "failed"


def test_finishing_without_changes_fails(repo: Path, store: TraceStore, tmp_path: Path) -> None:
    sandbox = FakeSandbox(result())
    finish = tool_turn("finish", FINISH, tool_id="t1")
    outcome = run_with_fake(repo, store, tmp_path, [finish], sandbox)
    assert outcome.status == "failed"
    assert "without changing anything" in outcome.error


def test_agent_failure_skips_the_test_run(repo: Path, store: TraceStore, tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    script = [text_turn("a"), text_turn("b"), text_turn("c")]
    outcome = run_with_fake(repo, store, tmp_path, script, sandbox)
    assert outcome.status == "failed"
    assert sandbox.calls == []
    assert store.get_run(outcome.run_id)["status"] == "failed"


def test_crash_is_recorded_on_the_run(repo: Path, store: TraceStore, tmp_path: Path) -> None:
    with pytest.raises(Exception, match="no scripted turn"):
        run_with_fake(repo, store, tmp_path, [], FakeSandbox())
    assert store.list_runs()[0]["status"] == "crashed"
