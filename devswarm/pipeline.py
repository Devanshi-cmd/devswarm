"""Wire real agents, a sandbox and git together, then run the orchestrator."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path

from git.exc import GitCommandError

from devswarm.agents.base import EventHandler
from devswarm.agents.coder import CoderAgent
from devswarm.agents.planner import PlannerAgent
from devswarm.agents.reviewer import ReviewerAgent
from devswarm.agents.tester import TesterAgent
from devswarm.config import DevSwarmConfig
from devswarm.github.client import GitHubClient, GitHubError, IssueRef, git_auth_env
from devswarm.github.local import (
    GitRepoState,
    LocalWorkspace,
    WorkspaceError,
    branch_name,
    clone_for_issue,
    commit_all,
    export_branch,
    last_commit_diff,
    prepare_local_workspace,
    push_branch,
)
from devswarm.github.pr import pr_title, quiet_references, render_pr_body, upstream_compare_url
from devswarm.llm.types import LLMClient
from devswarm.orchestrator.budget import Budget, BudgetLimits
from devswarm.orchestrator.engine import (
    Agents,
    FinalizeError,
    Finalizer,
    Orchestrator,
    OrchestratorResult,
    RunSummary,
)
from devswarm.orchestrator.state_machine import MachineConfig, State
from devswarm.orchestrator.verify import SandboxTestRunner
from devswarm.sandbox.base import Sandbox
from devswarm.sandbox.docker import DockerSandbox
from devswarm.sandbox.images import detect_profile, ensure_image
from devswarm.storage.redact import Redactor
from devswarm.storage.trace import TraceStore
from devswarm.tools.base import ToolContext
from devswarm.tools.paths import Workspace
from devswarm.tools.registry import default_registry

SandboxFactory = Callable[[Path, str], AbstractContextManager[Sandbox]]


@dataclass(frozen=True)
class RunOptions:
    llm: LLMClient
    store: TraceStore
    work_root: Path
    config: DevSwarmConfig = DevSwarmConfig()
    limits: BudgetLimits | None = None
    machine: MachineConfig | None = None
    sandbox_factory: SandboxFactory = DockerSandbox
    image_resolver: Callable[[Path], str] = ensure_image
    on_event: EventHandler | None = None
    on_progress: Callable[[str], None] | None = None


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    workspace: LocalWorkspace
    result: OrchestratorResult
    diff_path: Path | None
    failure_report_path: Path | None
    pr_url: str | None = None
    pr_body_path: Path | None = None


@contextmanager
def _records_crash(store: TraceStore, run_id: str) -> Iterator[None]:
    try:
        yield
    except BaseException as exc:
        store.finish_run(run_id, "crashed", error=f"{type(exc).__name__}: {exc}")
        raise


def _execute(
    run_id: str,
    task: str,
    workspace: LocalWorkspace,
    options: RunOptions,
    finalizer_for: Callable[[Budget], Finalizer],
) -> tuple[OrchestratorResult, Path | None]:
    profile = detect_profile(workspace.path)
    budget = Budget(options.limits)
    config = options.config

    with options.sandbox_factory(workspace.path, options.image_resolver(workspace.path)) as sandbox:
        tool_context = ToolContext(Workspace(workspace.path), sandbox)
        common = (
            options.llm,
            default_registry(),
            tool_context,
            options.store,
            budget,
            options.on_event,
        )
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
            finalize=finalizer_for(budget),
            store=options.store,
            budget=budget,
            config=options.machine,
            on_progress=options.on_progress,
        ).run(task)

    report_path = None
    if result.failure_report:
        report_path = workspace.path.parent / "failure_report.md"
        report_path.write_text(result.failure_report)
    options.store.finish_run(run_id, result.status.value, error=result.reason)
    return result, report_path


def run_locally(*, task: str, repo: Path, options: RunOptions) -> RunOutcome:
    """Fix a task in a local repository and leave the result on a branch."""
    store = options.store
    run_id = store.start_run(task, repo=str(repo), config=options.config.model_dump())
    with _records_crash(store, run_id):
        run_directory = (options.work_root / run_id).resolve()
        workspace = prepare_local_workspace(repo, run_directory / "repo", branch_name(task, run_id))
        diff_path = run_directory / "changes.diff"

        def finalizer_for(budget: Budget) -> Finalizer:
            def finalize(summary: RunSummary) -> None:
                try:
                    if not commit_all(workspace, f"{task}\n\n{summary.coder_summary}"):
                        raise FinalizeError("there was nothing to commit")
                    diff_path.write_text(last_commit_diff(workspace) + "\n")
                    export_branch(workspace)
                except (WorkspaceError, GitCommandError) as exc:
                    raise FinalizeError(str(exc)) from exc

            return finalize

        result, report_path = _execute(run_id, task, workspace, options, finalizer_for)
    return RunOutcome(
        run_id,
        workspace,
        result,
        diff_path if result.status is State.DONE else None,
        report_path,
    )


def run_issue(
    *,
    ref: IssueRef,
    github: GitHubClient,
    token: str,
    dry_run: bool,
    options: RunOptions,
    redactor: Redactor | None = None,
    on_run_started: Callable[[str], None] | None = None,
    fork: str | None = None,
) -> RunOutcome:
    """Fix a GitHub issue and open a pull request, or stop just before that if dry_run."""
    clean = redactor or Redactor()
    issue = github.get_issue(ref)
    if issue.is_pull_request:
        raise GitHubError(f"{ref} is a pull request, not an issue")
    if issue.state != "open":
        raise GitHubError(f"{ref} is {issue.state}; only open issues can be worked on")

    task = f"{issue.title}\n\n{issue.body}".strip()
    store = options.store
    run_id = store.start_run(task, repo=str(ref), config=options.config.model_dump())
    pr_urls: list[str] = []
    if on_run_started:
        on_run_started(run_id)

    with _records_crash(store, run_id):
        auth = git_auth_env(token)
        run_directory = (options.work_root / run_id).resolve()
        branch = f"devswarm/issue-{ref.number}"
        # In a fork, the code comes from and goes back to the fork. The issue stays upstream.
        fork_owner, _, fork_repo = (fork or "").partition("/")
        target = IssueRef(fork_owner, fork_repo, ref.number) if fork else ref
        workspace = clone_for_issue(github.clone_url(target), run_directory / "repo", branch, auth)
        diff_path = run_directory / "changes.diff"
        body_path = run_directory / "pr_body.md"

        def finalizer_for(budget: Budget) -> Finalizer:
            def finalize(summary: RunSummary) -> None:
                in_fork = fork is not None
                title = clean.text(pr_title(ref, issue.title, in_fork=in_fork))
                body = clean.text(render_pr_body(ref, summary, budget.spent_usd, in_fork=in_fork))
                change = summary.coder_summary
                message = clean.text(
                    f"{title}\n\n{quiet_references(change) if in_fork else change}"
                )
                try:
                    if not commit_all(workspace, message):
                        raise FinalizeError("there was nothing to commit")
                    diff_path.write_text(last_commit_diff(workspace) + "\n")
                    body_path.write_text(body)
                    if dry_run:
                        return
                    push_branch(workspace, issue.default_branch, auth)
                    pr_urls.append(
                        github.create_pull(
                            target, title=title, body=body, head=branch, base=issue.default_branch
                        )
                    )
                    (run_directory / "pr_url.txt").write_text(pr_urls[0])
                    if fork is not None:
                        (run_directory / "upstream_url.txt").write_text(
                            upstream_compare_url(ref, fork, issue.default_branch, branch)
                        )
                except (WorkspaceError, GitHubError, GitCommandError) as exc:
                    raise FinalizeError(clean.text(str(exc))) from exc

            return finalize

        result, report_path = _execute(run_id, task, workspace, options, finalizer_for)

    done = result.status is State.DONE
    return RunOutcome(
        run_id,
        workspace,
        result,
        diff_path if done else None,
        report_path,
        pr_url=pr_urls[0] if pr_urls else None,
        pr_body_path=body_path if done else None,
    )
