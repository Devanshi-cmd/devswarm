"""Run tasks across configurations, score them, and report the results."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from devswarm.evaluation.runners import RunResult, TaskRunner, remove_tree
from devswarm.evaluation.tasks import EvalTask
from devswarm.sandbox.base import Sandbox, SandboxError
from devswarm.sandbox.docker import DockerSandbox
from devswarm.sandbox.images import ensure_image

_SCORE_TIMEOUT_SECONDS = 120
_HIDDEN_DIRECTORY = "_hidden_tests"
_COPY_IGNORED = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", _HIDDEN_DIRECTORY)
# -c /dev/null and --noconftest stop a repo's own config or conftest.py from skipping
# or altering the hidden tests. Without them, editing one file could fake a pass.
_SCORE_COMMAND = ("pytest", "-q", "-c", "/dev/null", "--noconftest", "--rootdir=/workspace")

ConfigKind = Literal["devswarm", "claude_code"]
OutcomeStatus = Literal["solved", "unsolved", "error", "skipped", "peeked"]
SandboxFactory = Callable[[Path, str], AbstractContextManager[Sandbox]]


@dataclass(frozen=True)
class EvalConfig:
    name: str
    description: str
    kind: ConfigKind
    reviewer_enabled: bool = True


CONFIGS: dict[str, EvalConfig] = {
    config.name: config
    for config in (
        EvalConfig("devswarm-haiku", "DevSwarm on Haiku, with the Reviewer", "devswarm"),
        EvalConfig(
            "devswarm-haiku-no-reviewer",
            "DevSwarm on Haiku, without the Reviewer",
            "devswarm",
            reviewer_enabled=False,
        ),
        EvalConfig("claude-code-sonnet", "Headless Claude Code on Sonnet, alone", "claude_code"),
    )
}


class Scorer(Protocol):
    def score(self, task: EvalTask, final_tree: Path) -> bool: ...


class HiddenTestScorer:
    """Runs a task's hidden tests against a finished tree, in a fresh sandbox.

    The hidden tests only come together with the code here, after every agent has stopped.
    """

    def __init__(
        self,
        sandbox_factory: SandboxFactory = DockerSandbox,
        image_resolver: Callable[[Path], str] = ensure_image,
    ) -> None:
        self._sandbox_factory = sandbox_factory
        self._image_resolver = image_resolver

    def score(self, task: EvalTask, final_tree: Path) -> bool:
        with tempfile.TemporaryDirectory(prefix="devswarm-score-") as directory:
            target = Path(directory) / "repo"
            shutil.copytree(final_tree, target, symlinks=True, ignore=_COPY_IGNORED)
            shutil.copytree(task.hidden_path, target / _HIDDEN_DIRECTORY)
            with self._sandbox_factory(target, self._image_resolver(target)) as sandbox:
                result = sandbox.exec(
                    [*_SCORE_COMMAND, _HIDDEN_DIRECTORY], timeout=_SCORE_TIMEOUT_SECONDS
                )
        return result.exit_code == 0


@dataclass(frozen=True)
class TaskOutcome:
    task_id: str
    config: str
    status: OutcomeStatus
    iterations: int | None
    tokens: int
    cost_usd: float
    seconds: float
    detail: str | None = None


def _judge(task: EvalTask, config: str, result: RunResult, scorer: Scorer) -> TaskOutcome:
    status: OutcomeStatus
    detail = result.error
    if result.peeked:
        status = "peeked"
        detail = "transcript shows it reached outside its working copy"
    elif result.final_tree is None:
        status = "unsolved"
    else:
        try:
            status = "solved" if scorer.score(task, result.final_tree) else "unsolved"
            if status == "unsolved":
                detail = "hidden tests failed"
        except SandboxError as exc:
            status = "error"
            detail = str(exc)
    if result.discard_tree and result.final_tree is not None:
        remove_tree(result.final_tree)
    return TaskOutcome(
        task.id,
        config,
        status,
        result.iterations,
        result.tokens,
        result.cost_usd,
        result.seconds,
        detail,
    )


def run_eval(
    tasks: Sequence[EvalTask],
    runners: Mapping[str, TaskRunner],
    scorer: Scorer,
    *,
    max_api_usd: float,
    api_configs: Sequence[str],
    on_outcome: Callable[[TaskOutcome, list[TaskOutcome]], None] | None = None,
) -> list[TaskOutcome]:
    """Run every task under every configuration, one task at a time.

    Going task by task means a run stopped early still compares configs on the same
    tasks. The spending limit counts only configs billed to the API.
    """
    outcomes: list[TaskOutcome] = []
    api_spent = 0.0
    for task in tasks:
        for config, runner in runners.items():
            if config in api_configs and api_spent >= max_api_usd:
                reason = f"API spending limit of ${max_api_usd:.2f} reached"
                outcome = TaskOutcome(task.id, config, "skipped", None, 0, 0.0, 0.0, reason)
            else:
                outcome = _judge(task, config, runner.run(task), scorer)
                if config in api_configs:
                    api_spent += outcome.cost_usd
            outcomes.append(outcome)
            if on_outcome:
                on_outcome(outcome, outcomes)
    return outcomes


def _usd(value: float) -> str:
    return f"${value:.4f}"


def render_report(outcomes: Sequence[TaskOutcome], configs: Sequence[EvalConfig]) -> str:
    task_ids = list(dict.fromkeys(outcome.task_id for outcome in outcomes))
    lookup = {(outcome.task_id, outcome.config): outcome for outcome in outcomes}
    names = [config.name for config in configs]

    lines = [
        "# DevSwarm evaluation",
        "",
        f"{len(task_ids)} tasks. A task counts as solved only if the system finished "
        "and the task's hidden tests pass.",
        "",
        "## Solve rate",
        "",
        "| Configuration | Solved | Solve rate | Avg iterations | Tokens | Cost | Time |",
        "|---|---|---|---|---|---|---|",
    ]
    for config in configs:
        own = [outcome for outcome in outcomes if outcome.config == config.name]
        attempted = [outcome for outcome in own if outcome.status != "skipped"]
        solved = sum(outcome.status == "solved" for outcome in attempted)
        rate = f"{solved / len(attempted):.0%}" if attempted else "-"
        iterations = [o.iterations for o in attempted if o.iterations is not None]
        average = f"{sum(iterations) / len(iterations):.1f}" if iterations else "-"
        lines.append(
            f"| {config.name} | {solved}/{len(attempted)} | {rate} | {average} "
            f"| {sum(o.tokens for o in attempted):,} "
            f"| {_usd(sum(o.cost_usd for o in attempted))} "
            f"| {sum(o.seconds for o in attempted):.0f}s |"
        )

    lines += [
        "",
        "## By task",
        "",
        "| Task | " + " | ".join(names) + " |",
        "|---|" + "---|" * len(names),
    ]
    for task_id in task_ids:
        cells = [
            lookup[(task_id, name)].status if (task_id, name) in lookup else "-" for name in names
        ]
        lines.append(f"| {task_id} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## Details",
        "",
        "| Task | Configuration | Result | Iterations | Tokens | Cost | Time | Note |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for outcome in outcomes:
        shown = "-" if outcome.iterations is None else str(outcome.iterations)
        note = (outcome.detail or "").replace("|", "/").replace("\n", " ")[:160]
        lines.append(
            f"| {outcome.task_id} | {outcome.config} | {outcome.status} | {shown} "
            f"| {outcome.tokens:,} | {_usd(outcome.cost_usd)} | {outcome.seconds:.0f}s | {note} |"
        )

    lines += ["", "## Configurations", ""]
    lines += [f"- **{config.name}:** {config.description}" for config in configs]
    if any(config.kind == "claude_code" for config in configs):
        lines += [
            "",
            "Claude Code's cost is the figure it reports for itself. It runs on a Claude "
            "subscription login here, so that amount is notional and was not billed.",
        ]
    return "\n".join(lines) + "\n"
