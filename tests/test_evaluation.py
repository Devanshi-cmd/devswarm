import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeSandbox, result
from test_pipeline import approve, coder_finish, plan_turn, tester_finish

from devswarm.evaluation.harness import (
    CONFIGS,
    HiddenTestScorer,
    TaskOutcome,
    render_report,
    run_eval,
)
from devswarm.evaluation.runners import (
    ClaudeCodeRunner,
    DevSwarmRunner,
    RunResult,
    claude_environment,
)
from devswarm.evaluation.tasks import EvalTask, TaskError, load_tasks
from devswarm.llm.mock import MockLLMClient, tool_turn
from devswarm.orchestrator.state_machine import MachineConfig
from devswarm.pipeline import RunOptions
from devswarm.sandbox.base import SandboxError
from devswarm.storage.redact import Redactor
from devswarm.storage.trace import TraceStore

TASKS_DIR = Path(__file__).parent.parent / "evals" / "tasks"
ALL_TASKS = load_tasks(TASKS_DIR)
SENTINEL = "HIDDEN_SENTINEL_9f3a"


@pytest.fixture(autouse=True)
def temporary_files_stay_in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def _pytest(directory: Path, *targets: str) -> int:
    env = {**os.environ, "PYTHONPATH": str(directory), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-c",
            os.devnull,
            "--noconftest",
            f"--rootdir={directory}",
            "-p",
            "no:cacheprovider",
            *targets,
        ],
        cwd=directory,
        env=env,
        capture_output=True,
        check=False,
    ).returncode


def _with_hidden(task: EvalTask, tmp_path: Path) -> Path:
    target = tmp_path / "work"
    shutil.copytree(task.repo_path, target)
    shutil.copytree(task.hidden_path, target / "_hidden_tests")
    return target


def _apply_solution(task: EvalTask, directory: Path) -> None:
    for edit in json.loads((task.root / "solution.json").read_text()):
        path = directory / edit["file"]
        text = path.read_text()
        assert text.count(edit["old"]) == 1, f"{task.id}: {edit['old']!r}"
        path.write_text(text.replace(edit["old"], edit["new"]))


def test_there_are_at_least_ten_tasks() -> None:
    assert len(ALL_TASKS) >= 10


def test_tasks_cover_different_kinds_of_bug() -> None:
    assert len({task.category for task in ALL_TASKS}) >= 5


@pytest.mark.parametrize("task", ALL_TASKS, ids=lambda task: task.id)
def test_visible_tests_pass_on_the_buggy_code(task: EvalTask, tmp_path: Path) -> None:
    # An issue is only a fair challenge if the bug isn't already caught by a failing test.
    target = _with_hidden(task, tmp_path)
    shutil.rmtree(target / "_hidden_tests")
    assert _pytest(target) == 0


@pytest.mark.parametrize("task", ALL_TASKS, ids=lambda task: task.id)
def test_hidden_tests_fail_on_the_buggy_code(task: EvalTask, tmp_path: Path) -> None:
    assert _pytest(_with_hidden(task, tmp_path), "_hidden_tests") == 1


@pytest.mark.parametrize("task", ALL_TASKS, ids=lambda task: task.id)
def test_reference_solution_passes_everything(task: EvalTask, tmp_path: Path) -> None:
    target = _with_hidden(task, tmp_path)
    _apply_solution(task, target)
    assert _pytest(target, "_hidden_tests") == 0
    shutil.rmtree(target / "_hidden_tests")
    assert _pytest(target) == 0


@pytest.mark.parametrize("task", ALL_TASKS, ids=lambda task: task.id)
def test_repo_folder_contains_nothing_from_the_answer(task: EvalTask) -> None:
    assert not (task.repo_path / "hidden").exists()
    assert not (task.repo_path / "solution.json").exists()
    hidden_text = (task.hidden_path / "test_hidden.py").read_text()
    for path in task.repo_path.iterdir():
        assert path.read_text() != hidden_text


def make_task(tmp_path: Path) -> EvalTask:
    root = tmp_path / "tasks" / "sample"
    (root / "repo").mkdir(parents=True)
    (root / "hidden").mkdir()
    (root / "repo" / "calc.py").write_text("x = 1\n")
    (root / "hidden" / "test_hidden.py").write_text(f"# {SENTINEL}\ndef test_x():\n    pass\n")
    (root / "issue.md").write_text("Change x to 2.\n")
    (root / "task.toml").write_text('category = "sample"\n')
    (root / "solution.json").write_text(f'["{SENTINEL}"]\n')
    return load_tasks(tmp_path / "tasks")[0]


def test_load_rejects_incomplete_tasks(tmp_path: Path) -> None:
    (tmp_path / "broken" / "repo").mkdir(parents=True)
    with pytest.raises(TaskError, match="missing"):
        load_tasks(tmp_path)


def test_load_can_select_tasks(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    assert load_tasks(tmp_path / "tasks", only=[task.id]) == [task]
    with pytest.raises(TaskError, match="unknown"):
        load_tasks(tmp_path / "tasks", only=["nope"])


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TraceStore]:
    with TraceStore(tmp_path / "t.sqlite3", Redactor()) as trace:
        yield trace


@contextmanager
def _context(sandbox: FakeSandbox) -> Iterator[FakeSandbox]:
    yield sandbox


def _everything_under(path: Path) -> str:
    return "\n".join(
        str(item) + (item.read_text(errors="replace") if item.is_file() else "")
        for item in path.rglob("*")
        if ".git" not in item.parts
    )


def test_hidden_tests_never_reach_devswarm_agents(tmp_path: Path, store: TraceStore) -> None:
    task = make_task(tmp_path)
    edit = {"path": "calc.py", "old_str": "x = 1", "new_str": "x = 2"}
    script = [plan_turn(), tool_turn("edit_file", edit), coder_finish(), tester_finish(), approve()]
    llm = MockLLMClient(script)
    mounted: list[Path] = []
    sandbox = FakeSandbox(result())

    def factory(path: Path, image: str) -> Any:
        mounted.append(path)
        return _context(sandbox)

    runner = DevSwarmRunner(
        RunOptions(
            llm=llm,
            store=store,
            work_root=tmp_path / "work",
            sandbox_factory=factory,
            image_resolver=lambda path: "image",
            test_baseline=False,
        ),
        MachineConfig(),
    )
    outcome = runner.run(task)
    assert outcome.final_tree is not None

    assert SENTINEL not in _everything_under(mounted[0])
    assert not list(mounted[0].rglob("test_hidden*"))
    assert not list(mounted[0].rglob("solution.json"))
    prompts = json.dumps([str(request) for request in llm.requests])
    assert SENTINEL not in prompts
    assert "test_hidden" not in prompts


class FakeClaude:
    def __init__(self, stdout: str | None = None, edit: bool = True) -> None:
        self.stdout = stdout
        self.edit = edit
        self.argv: list[str] = []
        self.kwargs: dict[str, Any] = {}
        self.seen_tree = ""

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.argv = argv
        self.kwargs = kwargs
        self.seen_tree = _everything_under(Path(kwargs["cwd"]))
        if self.edit:
            (Path(kwargs["cwd"]) / "calc.py").write_text("x = 2\n")
        stdout = self.stdout
        if stdout is None:
            event = {
                "type": "result",
                "is_error": False,
                "total_cost_usd": 0.125,
                "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100},
            }
            stdout = '{"type":"system"}\n' + json.dumps(event) + "\n"
        return subprocess.CompletedProcess(argv, 0, stdout, "")


def test_claude_runs_sonnet_with_the_right_limits(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    fake = FakeClaude()
    ClaudeCodeRunner(command_runner=fake, executable="claude").run(task)
    argv = fake.argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--max-budget-usd") + 1] == "1.0"
    assert "--no-session-persistence" in argv
    assert "Bash" in argv[argv.index("--tools") + 1]
    assert fake.kwargs["stdin"] == subprocess.DEVNULL


def test_claude_never_receives_an_api_key_or_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-appear")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-should-not-appear")
    task = make_task(tmp_path)
    fake = FakeClaude()
    ClaudeCodeRunner(command_runner=fake, executable="claude").run(task)
    env = fake.kwargs["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "should-not-appear" not in json.dumps(env)
    assert set(claude_environment()) == {"HOME", "PATH", "LANG", "TERM"}


def test_claude_works_on_a_copy_without_the_answer(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    fake = FakeClaude()
    outcome = ClaudeCodeRunner(command_runner=fake, executable="claude").run(task)

    assert SENTINEL not in fake.seen_tree
    assert SENTINEL not in " ".join(fake.argv)
    assert "test_hidden" not in " ".join(fake.argv)
    assert not Path(fake.kwargs["cwd"]).is_relative_to(TASKS_DIR.parent.parent)
    assert (task.repo_path / "calc.py").read_text() == "x = 1\n"
    assert outcome.final_tree is not None
    assert outcome.final_tree.joinpath("calc.py").read_text() == "x = 2\n"
    shutil.rmtree(outcome.final_tree.parent)


def test_claude_result_is_parsed(tmp_path: Path) -> None:
    outcome = ClaudeCodeRunner(command_runner=FakeClaude(), executable="claude").run(
        make_task(tmp_path)
    )
    assert (outcome.tokens, outcome.cost_usd, outcome.error) == (115, 0.125, None)
    assert outcome.discard_tree
    assert outcome.final_tree is not None
    shutil.rmtree(outcome.final_tree.parent)


def test_claude_reporting_an_error_is_not_delivered(tmp_path: Path) -> None:
    stdout = json.dumps({"type": "result", "is_error": True, "subtype": "error_max_budget_usd"})
    outcome = ClaudeCodeRunner(command_runner=FakeClaude(stdout), executable="claude").run(
        make_task(tmp_path)
    )
    assert outcome.final_tree is None
    assert outcome.error == "error_max_budget_usd"


def test_claude_with_no_result_is_a_failure(tmp_path: Path) -> None:
    outcome = ClaudeCodeRunner(command_runner=FakeClaude("garbage"), executable="claude").run(
        make_task(tmp_path)
    )
    assert outcome.final_tree is None
    assert "no result" in (outcome.error or "")


def test_claude_timeout_is_a_failure(tmp_path: Path) -> None:
    def hang(argv: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, 1)

    outcome = ClaudeCodeRunner(command_runner=hang, executable="claude").run(make_task(tmp_path))
    assert outcome.error == "claude timed out"


def test_claude_reaching_for_the_answer_is_flagged(tmp_path: Path) -> None:
    event = {"type": "result", "is_error": False, "usage": {}}
    stdout = '{"tool":"Bash","cmd":"cat ../evals/tasks/x/hidden/test_hidden.py"}\n' + json.dumps(
        event
    )
    outcome = ClaudeCodeRunner(command_runner=FakeClaude(stdout), executable="claude").run(
        make_task(tmp_path)
    )
    assert outcome.peeked
    assert outcome.final_tree is not None
    shutil.rmtree(outcome.final_tree.parent)


def test_no_devswarm_config_uses_sonnet_over_the_api() -> None:
    assert "claude-code-sonnet" in CONFIGS
    assert CONFIGS["claude-code-sonnet"].kind == "claude_code"
    assert not [
        name for name, config in CONFIGS.items() if "sonnet" in name and config.kind == "devswarm"
    ]


class FakeScorer:
    def __init__(self, solved: set[str] | None = None) -> None:
        self.solved = solved
        self.calls: list[str] = []

    def score(self, task: EvalTask, final_tree: Path) -> bool:
        self.calls.append(task.id)
        return self.solved is None or task.id in self.solved


class FakeRunner:
    def __init__(self, tree: Path | None, cost: float = 0.1, **kwargs: Any) -> None:
        self.tree = tree
        self.cost = cost
        self.kwargs = kwargs
        self.calls = 0

    def run(self, task: EvalTask) -> RunResult:
        self.calls += 1
        return RunResult(self.tree, 1, 100, self.cost, 2.0, **self.kwargs)


def _tasks(tmp_path: Path, count: int) -> list[EvalTask]:
    return [EvalTask(f"t{i}", "c", "issue", tmp_path) for i in range(count)]


def _run(tasks: list[EvalTask], runners: dict[str, Any], scorer: Any, limit: float = 5.0) -> Any:
    return run_eval(tasks, runners, scorer, max_api_usd=limit, api_configs=["api"])


def test_solved_requires_passing_hidden_tests(tmp_path: Path) -> None:
    tasks = _tasks(tmp_path, 2)
    outcomes = _run(tasks, {"api": FakeRunner(tmp_path)}, FakeScorer({"t0"}))
    assert [outcome.status for outcome in outcomes] == ["solved", "unsolved"]
    assert outcomes[1].detail == "hidden tests failed"


def test_undelivered_result_is_unsolved_without_scoring(tmp_path: Path) -> None:
    scorer = FakeScorer()
    outcomes = _run(_tasks(tmp_path, 1), {"api": FakeRunner(None, error="gave up")}, scorer)
    assert outcomes[0].status == "unsolved"
    assert outcomes[0].detail == "gave up"
    assert scorer.calls == []


def test_peeking_never_counts_as_solved(tmp_path: Path) -> None:
    scorer = FakeScorer()
    outcomes = _run(_tasks(tmp_path, 1), {"api": FakeRunner(tmp_path, peeked=True)}, scorer)
    assert outcomes[0].status == "peeked"
    assert scorer.calls == []


def test_scorer_failure_is_an_error_not_a_miss(tmp_path: Path) -> None:
    class Broken:
        def score(self, task: EvalTask, final_tree: Path) -> bool:
            raise SandboxError("docker died")

    outcomes = _run(_tasks(tmp_path, 1), {"api": FakeRunner(tmp_path)}, Broken())
    assert outcomes[0].status == "error"


def test_api_spending_limit_stops_api_configs_only(tmp_path: Path) -> None:
    api = FakeRunner(tmp_path, cost=1.0)
    claude = FakeRunner(tmp_path, cost=9.0)
    outcomes = _run(_tasks(tmp_path, 3), {"api": api, "claude": claude}, FakeScorer(), limit=2.0)
    assert api.calls == 2
    assert claude.calls == 3
    assert [o.status for o in outcomes if o.config == "api"] == ["solved", "solved", "skipped"]


def test_each_outcome_is_reported_as_it_arrives(tmp_path: Path) -> None:
    seen: list[int] = []
    run_eval(
        _tasks(tmp_path, 2),
        {"api": FakeRunner(tmp_path)},
        FakeScorer(),
        max_api_usd=5,
        api_configs=["api"],
        on_outcome=lambda outcome, everything: seen.append(len(everything)),
    )
    assert seen == [1, 2]


def _scorer_with(exit_code: int, sandbox: FakeSandbox | None = None) -> tuple[Any, FakeSandbox]:
    box = sandbox or FakeSandbox(result(exit_code=exit_code))
    return HiddenTestScorer(lambda path, image: _context(box), lambda path: "image"), box


def test_scorer_uses_exit_code(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    assert _scorer_with(0)[0].score(task, task.repo_path) is True
    assert _scorer_with(1)[0].score(task, task.repo_path) is False


def test_scorer_ignores_the_repos_own_pytest_config(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    scorer, box = _scorer_with(0)
    scorer.score(task, task.repo_path)
    command = box.calls[0]
    assert "--noconftest" in command
    assert command[command.index("-c") + 1] == "/dev/null"
    assert command[-1] == "_hidden_tests"


def test_scorer_never_changes_the_final_tree(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    before = sorted(path.name for path in task.repo_path.iterdir())
    _scorer_with(0)[0].score(task, task.repo_path)
    assert sorted(path.name for path in task.repo_path.iterdir()) == before


def _outcome(task: str, config: str, status: Any, **kwargs: Any) -> TaskOutcome:
    values = {"iterations": 2, "tokens": 1000, "cost_usd": 0.01, "seconds": 10.0, **kwargs}
    return TaskOutcome(task, config, status, **values)


def test_report_compares_configs_side_by_side() -> None:
    outcomes = [
        _outcome("a", "devswarm-haiku", "solved"),
        _outcome("b", "devswarm-haiku", "unsolved"),
        _outcome("a", "claude-code-sonnet", "solved", iterations=None, cost_usd=0.5),
        _outcome("b", "claude-code-sonnet", "solved", iterations=None, cost_usd=0.5),
    ]
    report = render_report(outcomes, [CONFIGS["devswarm-haiku"], CONFIGS["claude-code-sonnet"]])
    assert "| devswarm-haiku | 1/2 | 50% | 2.0 | 2,000 | $0.0200 | 20s |" in report
    assert "| claude-code-sonnet | 2/2 | 100% | - | 2,000 | $1.0000 | 20s |" in report
    assert "| a | solved | solved |" in report
    assert "| b | unsolved | solved |" in report
    assert "notional" in report


def test_skipped_tasks_do_not_lower_the_solve_rate() -> None:
    outcomes = [
        _outcome("a", "devswarm-haiku", "solved"),
        _outcome("b", "devswarm-haiku", "skipped", tokens=0, cost_usd=0.0, seconds=0.0),
    ]
    assert "| 1/1 | 100% |" in render_report(outcomes, [CONFIGS["devswarm-haiku"]])


def test_report_note_cannot_break_the_table() -> None:
    outcomes = [_outcome("a", "devswarm-haiku", "error", detail="bad | pipe\nnewline")]
    report = render_report(outcomes, [CONFIGS["devswarm-haiku"]])
    assert "bad / pipe newline" in report
