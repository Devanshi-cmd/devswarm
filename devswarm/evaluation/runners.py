"""The systems being compared: DevSwarm, and headless Claude Code as a baseline."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from git.exc import GitCommandError

from devswarm.evaluation.tasks import EvalTask
from devswarm.github.local import WorkspaceError
from devswarm.orchestrator.state_machine import MachineConfig, State
from devswarm.pipeline import RunOptions, run_locally
from devswarm.sandbox.base import SandboxError

_CLAUDE_TIMEOUT_SECONDS = 1_200
_CLAUDE_TOOLS = "Bash,Read,Edit,Write,Glob,Grep"
# Any of these in Claude Code's transcript means it went looking outside its copy.
_PEEK_MARKERS = ("evals/tasks", "test_hidden", "solution.json", "_hidden_tests")


@dataclass(frozen=True)
class RunResult:
    final_tree: Path | None  # None when the system didn't deliver a finished result.
    iterations: int | None
    tokens: int
    cost_usd: float
    seconds: float
    error: str | None = None
    peeked: bool = False
    discard_tree: bool = False


class TaskRunner(Protocol):
    def run(self, task: EvalTask) -> RunResult: ...


class DevSwarmRunner:
    def __init__(self, options: RunOptions, machine: MachineConfig) -> None:
        self._options = replace(options, machine=machine)

    def run(self, task: EvalTask) -> RunResult:
        started = time.monotonic()
        try:
            outcome = run_locally(task=task.issue, repo=task.repo_path, options=self._options)
        except (SandboxError, WorkspaceError, GitCommandError) as exc:
            return RunResult(None, None, 0, 0.0, time.monotonic() - started, error=str(exc))

        run = self._options.store.get_run(outcome.run_id)
        tokens = (
            run["input_tokens"]
            + run["output_tokens"]
            + run["cache_read_tokens"]
            + run["cache_write_tokens"]
        )
        # A failed run delivers no pull request, so it can't count as solved
        # even if its working copy happens to pass the hidden tests.
        done = outcome.result.status is State.DONE
        return RunResult(
            final_tree=outcome.workspace.path if done else None,
            iterations=outcome.result.summary.iterations,
            tokens=tokens,
            cost_usd=run["cost_usd"],
            seconds=time.monotonic() - started,
            error=outcome.result.reason,
        )


CommandRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def claude_environment() -> dict[str, str]:
    """A minimal environment for headless Claude Code.

    Leaving out ANTHROPIC_API_KEY makes it use the machine's Claude Code login instead
    of calling Sonnet through the API, and keeps secrets away from its Bash tool.
    DevSwarm's virtualenv comes first so pytest is available, as it is in the sandbox.
    """
    home = Path.home()
    return {
        "HOME": str(home),
        "PATH": f"{Path(sys.prefix) / 'bin'}:{home / '.local' / 'bin'}:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
    }


class ClaudeCodeRunner:
    """Runs ``claude -p`` on a throwaway copy of the task's repo.

    It runs on the host because Claude Code needs its login and network. That is an
    exception to the sandbox rule, agreed for the eval tasks in this repository only.
    """

    def __init__(
        self,
        *,
        model: str = "sonnet",
        max_budget_usd: float = 1.0,
        command_runner: CommandRunner = subprocess.run,
        executable: str | None = None,
    ) -> None:
        self._model = model
        self._max_budget_usd = max_budget_usd
        self._command_runner = command_runner
        self._executable = executable or shutil.which("claude") or "claude"

    def _prompt(self, task: EvalTask) -> str:
        return (
            "Fix the following issue in the code in the current directory. "
            "Work without asking questions, and run the tests to check your change.\n\n"
            f"{task.issue}"
        )

    def run(self, task: EvalTask) -> RunResult:
        started = time.monotonic()
        # Under the system temp directory, well away from this repository's evals folder.
        copy = Path(tempfile.mkdtemp(prefix="devswarm-eval-")) / "repo"
        shutil.copytree(task.repo_path, copy, symlinks=True)
        try:
            completed = self._command_runner(
                [
                    self._executable,
                    "-p",
                    self._prompt(task),
                    "--model",
                    self._model,
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--no-session-persistence",
                    "--max-budget-usd",
                    str(self._max_budget_usd),
                    "--tools",
                    _CLAUDE_TOOLS,
                    "--allowedTools",
                    _CLAUDE_TOOLS,
                ],
                cwd=copy,
                env=claude_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=_CLAUDE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self._failed(copy, started, "claude timed out")
        except FileNotFoundError:
            return self._failed(copy, started, "the claude command is not installed")

        result = _result_event(completed.stdout)
        if result is None:
            return self._failed(copy, started, f"no result event: {completed.stderr[-500:]}")

        usage = result.get("usage") or {}
        tokens = sum(
            usage.get(field, 0)
            for field in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        )
        failed = bool(result.get("is_error"))
        return RunResult(
            final_tree=None if failed else copy,
            iterations=None,
            tokens=tokens,
            cost_usd=float(result.get("total_cost_usd") or 0.0),
            seconds=time.monotonic() - started,
            error=str(result.get("subtype")) if failed else None,
            peeked=any(marker in completed.stdout for marker in _PEEK_MARKERS),
            discard_tree=True,
        )

    @staticmethod
    def _failed(copy: Path, started: float, error: str) -> RunResult:
        shutil.rmtree(copy.parent, ignore_errors=True)
        return RunResult(None, None, 0, 0.0, time.monotonic() - started, error=error)


def _result_event(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None


def remove_tree(path: Path) -> None:
    # Claude Code's copy is one level inside its own temp directory.
    target = path.parent if path.parent.name.startswith("devswarm-eval-") else path
    if os.path.isdir(target):
        shutil.rmtree(target, ignore_errors=True)
