"""Local mode with the Coder alone. The M4 orchestrator replaces this flow."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from devswarm.agents.base import AgentContext, AgentResult, EventHandler
from devswarm.agents.coder import CoderAgent, CoderOutput
from devswarm.config import DevSwarmConfig
from devswarm.github.local import (
    LocalWorkspace,
    branch_name,
    commit_all,
    export_branch,
    last_commit_diff,
    prepare_local_workspace,
)
from devswarm.llm.types import LLMClient
from devswarm.orchestrator.budget import Budget, BudgetLimits
from devswarm.sandbox.base import Sandbox
from devswarm.sandbox.docker import DockerSandbox
from devswarm.sandbox.images import detect_profile, ensure_image
from devswarm.storage.trace import TraceStore
from devswarm.tools.base import ToolContext
from devswarm.tools.paths import Workspace
from devswarm.tools.registry import default_registry

_TEST_TIMEOUT_SECONDS = 300

SandboxFactory = Callable[[Path, str], AbstractContextManager[Sandbox]]


@dataclass(frozen=True)
class LocalRunOutcome:
    run_id: str
    status: str
    workspace: LocalWorkspace
    tests_passed: bool
    test_output: str
    diff_path: Path | None
    coder: AgentResult[CoderOutput]
    error: str | None = None


def run_coder_locally(
    *,
    task: str,
    repo: Path,
    llm: LLMClient,
    store: TraceStore,
    work_root: Path,
    config: DevSwarmConfig | None = None,
    limits: BudgetLimits | None = None,
    sandbox_factory: SandboxFactory = DockerSandbox,
    image_resolver: Callable[[Path], str] = ensure_image,
    on_event: EventHandler | None = None,
) -> LocalRunOutcome:
    config = config or DevSwarmConfig()
    run_id = store.start_run(task, repo=str(repo), config=config.model_dump())
    try:
        outcome = _run(
            run_id, task, repo, llm, store, work_root, config, limits,
            sandbox_factory, image_resolver, on_event,
        )  # fmt: skip
    except BaseException as exc:
        store.finish_run(run_id, "crashed", error=f"{type(exc).__name__}: {exc}")
        raise
    store.finish_run(run_id, outcome.status, error=outcome.error)
    return outcome


def _run(
    run_id: str,
    task: str,
    repo: Path,
    llm: LLMClient,
    store: TraceStore,
    work_root: Path,
    config: DevSwarmConfig,
    limits: BudgetLimits | None,
    sandbox_factory: SandboxFactory,
    image_resolver: Callable[[Path], str],
    on_event: EventHandler | None,
) -> LocalRunOutcome:
    run_directory = (work_root / run_id).resolve()
    workspace = prepare_local_workspace(repo, run_directory / "repo", branch_name(task, run_id))
    image = image_resolver(workspace.path)
    test_command = detect_profile(workspace.path).test_command()

    with sandbox_factory(workspace.path, image) as sandbox:
        coder = CoderAgent(
            config.coder,
            llm,
            default_registry(),
            ToolContext(Workspace(workspace.path), sandbox),
            store,
            Budget(limits),
            on_event,
        )
        result = coder.run(task, AgentContext(run_id=run_id))
        if result.status != "finished":
            return LocalRunOutcome(
                run_id, "failed", workspace, False, "", None, result, result.error
            )
        # We run the tests ourselves. The model saying they pass proves nothing.
        tests = sandbox.exec(test_command, timeout=_TEST_TIMEOUT_SECONDS)

    test_output = tests.stdout + tests.stderr
    if tests.exit_code != 0:
        error = f"tests failed with exit code {tests.exit_code}"
        return LocalRunOutcome(run_id, "failed", workspace, False, test_output, None, result, error)

    summary = result.output.summary if result.output else task
    if not commit_all(workspace, f"{task}\n\n{summary}"):
        error = "the agent finished without changing anything"
        return LocalRunOutcome(run_id, "failed", workspace, True, test_output, None, result, error)

    diff_path = run_directory / "changes.diff"
    diff_path.write_text(last_commit_diff(workspace) + "\n")
    export_branch(workspace)
    return LocalRunOutcome(run_id, "done", workspace, True, test_output, diff_path, result)
