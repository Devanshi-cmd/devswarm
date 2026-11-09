"""The ``devswarm`` command line."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from git.exc import GitCommandError
from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table

from devswarm import __version__
from devswarm.agents.base import AgentEvent, EventHandler
from devswarm.config import (
    AGENT_NAMES,
    DEFAULT_MODEL,
    DevSwarmConfig,
    db_path,
    get_secret,
    load_env,
    parse_agent_overrides,
    parse_effort_overrides,
)
from devswarm.dashboard.app import create_app
from devswarm.evaluation.harness import (
    CONFIGS,
    HiddenTestScorer,
    TaskOutcome,
    render_report,
    run_eval,
)
from devswarm.evaluation.runners import ClaudeCodeRunner, DevSwarmRunner, TaskRunner
from devswarm.evaluation.tasks import TaskError, load_tasks
from devswarm.github.client import (
    GitHubError,
    PyGithubClient,
    parse_issue_ref,
    resolve_github_token,
)
from devswarm.github.local import WorkspaceError
from devswarm.jobs.queue import JobQueue
from devswarm.jobs.worker import Worker
from devswarm.llm.client import AnthropicLLMClient
from devswarm.llm.mock import MockLLMClient, text_turn
from devswarm.llm.types import LLMClient, LLMError
from devswarm.orchestrator.budget import BudgetLimits
from devswarm.orchestrator.state_machine import MachineConfig, State
from devswarm.pipeline import RunOptions, run_issue, run_locally
from devswarm.sandbox.base import SandboxError
from devswarm.sandbox.check import run_checks
from devswarm.smoke import run_smoke
from devswarm.storage.redact import RedactingLogFilter, Redactor
from devswarm.storage.report import render_markdown
from devswarm.storage.trace import RunNotFoundError, TraceStore

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
console = Console()
error_console = Console(stderr=True)


def _store() -> TraceStore:
    return TraceStore(db_path(), Redactor.from_env())


def _fail(message: str) -> typer.Exit:
    error_console.print(f"[bold red]Error:[/] {message}", highlight=False)
    return typer.Exit(code=1)


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show debug logs.")] = False,
) -> None:
    load_env()
    handler = RichHandler(console=error_console, show_path=False)
    handler.addFilter(RedactingLogFilter(Redactor.from_env()))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(message)s",
        handlers=[handler],
        force=True,
    )


@app.command()
def version() -> None:
    """Print the DevSwarm version."""
    console.print(__version__)


@app.command()
def run(
    repo: Annotated[str | None, typer.Option(help="Path to a local repository.")] = None,
    task: Annotated[str | None, typer.Option(help="Task text, used with --repo.")] = None,
    issue: Annotated[str | None, typer.Option(help="GitHub issue, owner/repo#123.")] = None,
    dry_run: Annotated[bool, typer.Option(help="Do everything except push and PR.")] = False,
    model: Annotated[
        list[str] | None, typer.Option(help="Per-agent model, e.g. reviewer=claude-sonnet-5-5.")
    ] = None,
    effort: Annotated[
        list[str] | None, typer.Option(help="Per-agent effort, e.g. coder=high.")
    ] = None,
    max_usd: Annotated[float, typer.Option(help="Stop the run at this cost.")] = 1.00,
    max_iterations: Annotated[int, typer.Option(help="How many times the Coder may run.")] = 5,
    reviewer: Annotated[bool, typer.Option(help="Use the Reviewer agent.")] = True,
) -> None:
    """Fix a GitHub issue with a pull request, or a local task with a branch."""
    if bool(issue) == bool(repo and task):
        raise _fail("use either --issue, or --repo together with --task")
    try:
        config = DevSwarmConfig().with_overrides(
            parse_agent_overrides(model or []), parse_effort_overrides(effort or [])
        )
    except ValueError as exc:
        raise _fail(str(exc)) from exc

    table = Table(title="Resolved agent configuration")
    for column in ("Agent", "Model", "Effort", "Max turns"):
        table.add_column(column)
    for name in AGENT_NAMES:
        agent = config.for_agent(name)
        table.add_row(name, agent.model, agent.effort, str(agent.max_turns))
    console.print(table)
    issue_ref = None
    token = None
    if issue:
        try:
            issue_ref = parse_issue_ref(issue)
        except GitHubError as exc:
            raise _fail(str(exc)) from exc

    api_key = get_secret("ANTHROPIC_API_KEY")
    if api_key is None:
        raise _fail("ANTHROPIC_API_KEY is not set; add it to .env")
    if issue_ref is not None:
        token = resolve_github_token()
        if token is None:
            raise _fail("no GitHub token: set GITHUB_TOKEN in .env, or run gh auth login")

    redactor = Redactor.from_env(extra=[token] if token else [])
    with TraceStore(db_path(), redactor) as store:
        options = RunOptions(
            llm=AnthropicLLMClient.from_api_key(api_key),
            store=store,
            work_root=db_path().parent / "workspaces",
            config=config,
            limits=BudgetLimits(max_usd=max_usd),
            machine=MachineConfig(max_iterations=max_iterations, reviewer_enabled=reviewer),
            on_event=_event_printer(redactor),
            on_progress=lambda message: console.rule(f"[bold]{escape(message)}"),
        )
        try:
            if issue_ref is not None and token is not None:
                outcome = run_issue(
                    ref=issue_ref,
                    github=PyGithubClient(token),
                    token=token,
                    dry_run=dry_run,
                    options=options,
                    redactor=redactor,
                )
            elif repo is not None and task is not None:
                outcome = run_locally(task=task, repo=Path(repo), options=options)
            else:
                raise _fail("use either --issue, or --repo together with --task")
        except (SandboxError, WorkspaceError, GitHubError, GitCommandError) as exc:
            raise _fail(escape(redactor.text(str(exc)))) from exc
        cost = store.get_run(outcome.run_id)["cost_usd"]

    result = outcome.result
    tests = result.summary.tests
    review = result.summary.review
    summary = Table(title=f"Run {outcome.run_id}", show_header=False)
    summary.add_row("Status", result.status.value)
    summary.add_row("Tests", "not run" if tests is None else "passed" if tests.passed else "failed")
    summary.add_row("Reviewer", review.verdict if review else "did not review")
    summary.add_row("Iterations", str(result.summary.iterations))
    summary.add_row("Re-plans", str(result.summary.replans))
    summary.add_row("Cost", f"${cost:.6f}")
    summary.add_row("Branch", outcome.workspace.branch)
    summary.add_row("Working copy", str(outcome.workspace.path))
    if outcome.diff_path:
        summary.add_row("Diff", str(outcome.diff_path))
    if outcome.failure_report_path:
        summary.add_row("Failure report", str(outcome.failure_report_path))
    if outcome.pr_body_path:
        summary.add_row("PR body", str(outcome.pr_body_path))
    if outcome.pr_url:
        summary.add_row("Pull request", outcome.pr_url)
    elif dry_run and result.status is State.DONE:
        summary.add_row("Pull request", "skipped (dry run)")
    console.print(summary)
    if result.status is not State.DONE:
        raise _fail(escape(redactor.text(result.reason or "the run failed")))


def _event_printer(redactor: Redactor) -> EventHandler:
    def handle(event: AgentEvent) -> None:
        if event.kind == "turn":
            return
        style = "red" if event.is_error else "green" if event.kind == "finish" else "cyan"
        # escape: tool arguments can contain square brackets, which Rich reads as markup.
        console.print(f"[{style}]{event.agent}[/] {escape(redactor.text(event.detail))}")

    return handle


@app.command()
def smoke(
    live: Annotated[bool, typer.Option(help="Call the real API. Costs a fraction of a cent.")] = (
        False
    ),
    model: Annotated[str, typer.Option(help="Model to test.")] = DEFAULT_MODEL,
) -> None:
    """Hold a two-turn conversation and confirm caching and tracing work."""
    client: LLMClient
    if live:
        api_key = get_secret("ANTHROPIC_API_KEY")
        if api_key is None:
            raise _fail("ANTHROPIC_API_KEY is not set; add it to .env")
        client = AnthropicLLMClient.from_api_key(api_key)
    else:
        client = MockLLMClient([text_turn("one"), text_turn("two")])

    with _store() as store:
        try:
            result = run_smoke(client, store, model)
        except LLMError as exc:
            raise _fail(str(exc)) from exc

    table = Table(title=f"Smoke test ({'live API' if live else 'mock'}, {model})")
    for column in ("Turn", "Stop", "Input", "Output", "Cache read", "Cache write", "Cost"):
        table.add_column(column)
    for number, turn in enumerate(result.turns, start=1):
        table.add_row(
            str(number),
            turn.stop_reason,
            str(turn.usage.input_tokens),
            str(turn.usage.output_tokens),
            str(turn.usage.cache_read_input_tokens),
            str(turn.usage.cache_creation_input_tokens),
            f"${turn.cost_usd:.6f}",
        )
    console.print(table)
    console.print(f"Run [bold]{result.run_id}[/] recorded, total ${result.cost_usd:.6f}")
    if not result.cache_hit:
        raise _fail("turn 2 read nothing from the cache; something invalidated the prefix")
    console.print("[green]Cache hit confirmed on turn 2.[/]")


@app.command()
def runs() -> None:
    """List recorded runs, newest first."""
    with _store() as store:
        rows = store.list_runs()
    table = Table(title="DevSwarm runs")
    for column in ("Run", "Status", "Task", "LLM calls", "Cost", "Started"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            row["id"],
            row["status"],
            row["task"],
            str(row["llm_calls"]),
            f"${row['cost_usd']:.6f}",
            row["created_at"],
        )
    console.print(table)


@app.command()
def report(run_id: str) -> None:
    """Print a Markdown summary of a run."""
    with _store() as store:
        try:
            markdown = render_markdown(store, run_id)
        except RunNotFoundError:
            raise _fail(f"no run with id {run_id!r}") from None
    console.print(Markdown(markdown))


@app.command("sandbox-check")
def sandbox_check(
    repo: Annotated[Path, typer.Option(help="Repository to mount.")] = Path("."),
) -> None:
    """Start a real sandbox and verify each isolation guarantee."""
    try:
        results = run_checks(repo)
    except SandboxError as exc:
        raise _fail(str(exc)) from exc

    table = Table(title="Sandbox isolation")
    table.add_column("Guarantee")
    table.add_column("Result")
    for name, passed in results:
        table.add_row(name, "[green]pass[/]" if passed else "[red]FAIL[/]")
    console.print(table)
    failed = [name for name, passed in results if not passed]
    if failed:
        raise _fail(f"{len(failed)} guarantee(s) failed: {', '.join(failed)}")


@app.command()
def dashboard(
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8420,
    public_host: Annotated[
        str | None, typer.Option(help="Public hostname, when served behind a reverse proxy.")
    ] = None,
) -> None:
    """Start the web dashboard."""
    database = db_path()
    hosts = [host, "localhost", *([public_host] if public_host else [])]
    web_app = create_app(
        database,
        database.parent / "workspaces",
        allowed_hosts=hosts,
        jobs_path=database.parent / "jobs.sqlite3",
        access_code=get_secret("DEVSWARM_ACCESS_CODE"),
    )
    console.print(f"DevSwarm dashboard: [bold]http://{host}:{port}[/]")
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


@app.command()
def worker() -> None:
    """Run jobs submitted through the dashboard, one at a time."""
    api_key = get_secret("ANTHROPIC_API_KEY")
    if api_key is None:
        raise _fail("ANTHROPIC_API_KEY is not set; add it to .env")
    token = resolve_github_token()
    if token is None:
        raise _fail("no GitHub token: set GITHUB_TOKEN in .env, or run gh auth login")

    database = db_path()
    redactor = Redactor.from_env(extra=[token])
    with TraceStore(database, redactor) as store:
        job_worker = Worker(
            queue=JobQueue(database.parent / "jobs.sqlite3"),
            github=PyGithubClient(token),
            token=token,
            options=RunOptions(
                llm=AnthropicLLMClient.from_api_key(api_key),
                store=store,
                work_root=database.parent / "workspaces",
            ),
            redactor=redactor,
        )
        console.print("DevSwarm worker: waiting for jobs")
        job_worker.run_forever()


@app.command("eval")
def eval_command(
    config: Annotated[
        list[str] | None,
        typer.Option(help=f"Configuration to run, repeatable. Choose from: {', '.join(CONFIGS)}."),
    ] = None,
    task: Annotated[list[str] | None, typer.Option(help="Only run this task.")] = None,
    tasks_dir: Annotated[Path, typer.Option(help="Where the tasks live.")] = Path("evals/tasks"),
    max_api_usd: Annotated[
        float, typer.Option(help="Stop API-billed runs once they have spent this much.")
    ] = 3.00,
    output: Annotated[Path, typer.Option(help="Where to write the report.")] = Path(
        ".devswarm/eval_report.md"
    ),
) -> None:
    """Run the eval tasks and report the solve rate for each configuration."""
    names = config or ["devswarm-haiku"]
    unknown = [name for name in names if name not in CONFIGS]
    if unknown:
        raise _fail(f"unknown configuration: {', '.join(unknown)}")
    selected = [CONFIGS[name] for name in names]
    try:
        tasks = load_tasks(tasks_dir, only=task or ())
    except TaskError as exc:
        raise _fail(str(exc)) from exc

    api_configs = [item.name for item in selected if item.kind == "devswarm"]
    api_key = get_secret("ANTHROPIC_API_KEY")
    if api_configs and api_key is None:
        raise _fail("ANTHROPIC_API_KEY is not set; add it to .env")

    output.parent.mkdir(parents=True, exist_ok=True)
    with _store() as store:
        runners: dict[str, TaskRunner] = {}
        for item in selected:
            if item.kind == "claude_code":
                runners[item.name] = ClaudeCodeRunner()
                continue
            assert api_key is not None
            runners[item.name] = DevSwarmRunner(
                RunOptions(
                    llm=AnthropicLLMClient.from_api_key(api_key),
                    store=store,
                    work_root=db_path().parent / "workspaces",
                ),
                MachineConfig(reviewer_enabled=item.reviewer_enabled),
            )

        def on_outcome(outcome: TaskOutcome, everything: list[TaskOutcome]) -> None:
            # Written after every task, so an interrupted eval still leaves a report.
            output.write_text(render_report(everything, selected))
            console.print(
                f"{outcome.task_id} [cyan]{outcome.config}[/] {outcome.status} "
                f"${outcome.cost_usd:.4f} {outcome.seconds:.0f}s"
            )

        outcomes = run_eval(
            tasks,
            runners,
            HiddenTestScorer(),
            max_api_usd=max_api_usd,
            api_configs=api_configs,
            on_outcome=on_outcome,
        )
    console.print(Markdown(render_report(outcomes, selected)))
    console.print(f"Report written to {output}")
