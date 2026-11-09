"""Local mode: wire real agents, a sandbox and git together, then run the orchestrator."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from git.exc import GitCommandError

from devswarm.agents.base import EventHandler
from devswarm.agents.coder import CoderAgent
from devswarm.agents.planner import PlannerAgent
from devswarm.agents.reviewer import ReviewerAgent
from devswarm.agents.tester import TesterAgent
from devswarm.config import DevSwarmConfig
from devswarm.github.local import (
    GitRepoState,
    LocalWorkspace,
    WorkspaceError,
    branch_name,
    commit_all,
    export_branch,
    last_commit_diff,
    prepare_local_workspace,
)
from devswarm.llm.types import LLMClient
from devswarm.orchestrator.budget import Budget, BudgetLimits
from devswarm.orchestrator.engine import (
    Agents,
    FinalizeError,
    Orchestrator,
    OrchestratorResult,
    RunSummary,
)
from devswarm.orchestrator.state_machine import MachineConfig, State
from devswarm.orchestrator.verify import SandboxTestRunner
from devswarm.sandbox.base import Sandbox
from devswarm.sandbox.docker import DockerSandbox
from devswarm.sandbox.images import detect_profile, ensure_image
from devswarm.storage.trace import TraceStore
from devswarm.tools.base import ToolContext
from devswarm.tools.paths import Workspace
from devswarm.tools.registry import default_registry

SandboxFactory = Callable[[Path, str], AbstractContextManager[Sandbox]]


@dataclass(frozen=True)
class LocalRunOutcome:
    run_id: str
    workspace: LocalWorkspace
    result: OrchestratorResult
    diff_path: Path | None
    failure_report_path: Path | None


def run_locally(
    *,
    task: str,
    repo: Path,
    llm: LLMClient,
    store: TraceStore,
    work_root: Path,
    config: DevSwarmConfig | None = None,
    limits: BudgetLimits | None = None,
    machine: MachineConfig | None = None,
    sandbox_factory: SandboxFactory = DockerSandbox,
    image_resolver: Callable[[Path], str] = ensure_image,
    on_event: EventHandler | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> LocalRunOutcome:
    config = config or DevSwarmConfig()
    run_id = store.start_run(task, repo=str(repo), config=config.model_dump())
    try:
        run_directory = (work_root / run_id).resolve()
        workspace = prepare_local_workspace(repo, run_directory / "repo", branch_name(task, run_id))
        profile = detect_profile(workspace.path)
        budget = Budget(limits)
        diff_path = run_directory / "changes.diff"

        def finalize(summary: RunSummary) -> None:
            try:
                if not commit_all(workspace, f"{task}\n\n{summary.coder_summary}"):
                    raise FinalizeError("there was nothing to commit")
                diff_path.write_text(last_commit_diff(workspace) + "\n")
                export_branch(workspace)
            except (WorkspaceError, GitCommandError) as exc:
                raise FinalizeError(str(exc)) from exc

        with sandbox_factory(workspace.path, image_resolver(workspace.path)) as sandbox:
            registry = default_registry()
            tool_context = ToolContext(Workspace(workspace.path), sandbox)
            common = (llm, registry, tool_context, store, budget, on_event)
            result = Orchestrator(
                run_id=run_id,
                agents=Agents(
                    planner=PlannerAgent(config.planner, *common),
                    coder=CoderAgent(config.coder, *common),
                    tester=TesterAgent(config.tester, *common),
                    reviewer=ReviewerAgent(config.reviewer, *common),
                ),
                repo=GitRepoState(workspace, profile.is_test_path),
                tests=SandboxTestRunner(sandbox, profile),
                finalize=finalize,
                store=store,
                budget=budget,
                config=machine,
                on_progress=on_progress,
            ).run(task)
    except BaseException as exc:
        store.finish_run(run_id, "crashed", error=f"{type(exc).__name__}: {exc}")
        raise

    report_path = None
    if result.failure_report:
        report_path = run_directory / "failure_report.md"
        report_path.write_text(result.failure_report)
    store.finish_run(run_id, result.status.value, error=result.reason)
    return LocalRunOutcome(
        run_id,
        workspace,
        result,
        diff_path if result.status is State.DONE else None,
        report_path,
    )
