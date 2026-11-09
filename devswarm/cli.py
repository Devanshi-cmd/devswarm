"""The ``devswarm`` command line."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer
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
from devswarm.github.local import WorkspaceError
from devswarm.llm.client import AnthropicLLMClient
from devswarm.llm.mock import MockLLMClient, text_turn
from devswarm.llm.types import LLMClient, LLMError
from devswarm.orchestrator.budget import BudgetLimits
from devswarm.pipeline import run_coder_locally
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
) -> None:
    """Fix a task in a local repository and leave the result on a branch."""
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
    if issue or repo is None or task is None:
        raise _fail(f"the GitHub flow is not built yet (dry_run={dry_run}); it arrives in M5")

    api_key = get_secret("ANTHROPIC_API_KEY")
    if api_key is None:
        raise _fail("ANTHROPIC_API_KEY is not set; add it to .env")

    with _store() as store:
        try:
            outcome = run_coder_locally(
                task=task,
                repo=Path(repo),
                llm=AnthropicLLMClient.from_api_key(api_key),
                store=store,
                work_root=db_path().parent / "workspaces",
                config=config,
                limits=BudgetLimits(max_usd=max_usd),
                on_event=_event_printer(Redactor.from_env()),
            )
        except (SandboxError, WorkspaceError, GitCommandError) as exc:
            raise _fail(str(exc)) from exc
        cost = store.get_run(outcome.run_id)["cost_usd"]

    summary = Table(title=f"Run {outcome.run_id}", show_header=False)
    summary.add_row("Status", outcome.status)
    summary.add_row("Tests", "passed" if outcome.tests_passed else "not passed")
    summary.add_row("Agent turns", str(outcome.coder.turns))
    summary.add_row("Cost", f"${cost:.6f}")
    summary.add_row("Branch", outcome.workspace.branch)
    summary.add_row("Working copy", str(outcome.workspace.path))
    if outcome.diff_path:
        summary.add_row("Diff", str(outcome.diff_path))
    console.print(summary)
    if outcome.status != "done":
        raise _fail(outcome.error or "the run failed")


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
def dashboard() -> None:
    """Start the web dashboard (arrives in M6)."""
    raise _fail("the dashboard is not built yet; it arrives in M6")


@app.command("eval")
def eval_command() -> None:
    """Run the evaluation suite (arrives in M7)."""
    raise _fail("the eval harness is not built yet; it arrives in M7")
