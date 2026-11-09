"""Turn stored trace rows into the plain values the templates display."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from sqlite3 import Row
from typing import Any

from markupsafe import Markup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import DiffLexer

from devswarm.dashboard.charts import BarChart, Datum, Series, horizontal_bars
from devswarm.jobs.queue import Job, JobQueue
from devswarm.sandbox.output import clip
from devswarm.storage.trace import TraceStore

_MAX_DIFF_CHARS = 400_000

_GOOD = {"done", "finished", "passed"}
_BAD = {"failed", "crashed", "error", "refused"}
_RUNNING = {"running"}

_TOKEN_SERIES = (
    Series("cache_read_tokens", "Cache read", "series-1"),
    Series("cache_write_tokens", "Cache write", "series-2"),
    Series("input_tokens", "Input", "series-3"),
    Series("output_tokens", "Output", "series-4"),
)
_COST_SERIES = (Series("cost_usd", "Cost", "series-1"),)


def format_tokens(value: float) -> str:
    amount = int(value)
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}M"
    if amount >= 10_000:
        return f"{amount / 1_000:.1f}K"
    return f"{amount:,}"


def format_usd(value: float) -> str:
    return f"${value:.4f}"


def format_duration(started: str | None, finished: str | None) -> str:
    if not started or not finished:
        return "-"
    seconds = (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def format_time(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass(frozen=True)
class StatusView:
    label: str
    kind: str  # Selects the colour; the icon and label carry the meaning too.
    icon: str


def status_view(status: str) -> StatusView:
    label = status.replace("_", " ").capitalize()
    if status in _GOOD:
        return StatusView(label, "good", "✓")
    if status in _BAD:
        return StatusView(label, "critical", "✕")
    if status in _RUNNING:
        return StatusView(label, "running", "●")
    return StatusView(label, "serious", "!")


@dataclass(frozen=True)
class RunListItem:
    id: str
    status: StatusView
    title: str
    llm_calls: int
    cost: str
    started: str


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else "(no task)"


def run_list(store: TraceStore) -> list[RunListItem]:
    return [
        RunListItem(
            id=row["id"],
            status=status_view(row["status"]),
            title=_first_line(row["task"]),
            llm_calls=row["llm_calls"],
            cost=format_usd(row["cost_usd"]),
            started=format_time(row["created_at"]),
        )
        for row in store.list_runs()
    ]


@dataclass(frozen=True)
class StepView:
    id: int
    agent: str
    iteration: int
    status: StatusView
    is_running: bool
    summary: str | None
    has_details: bool
    llm_calls: int
    cost: str
    tokens: str
    duration: str


@dataclass(frozen=True)
class TableRow:
    agent: str
    cells: tuple[str, ...]


@dataclass(frozen=True)
class StageView:
    label: str
    state: str  # done, active, failed or pending.
    icon: str


@dataclass(frozen=True)
class ActivityView:
    agent: str
    tool: str
    detail: str
    is_error: bool


@dataclass(frozen=True)
class RunView:
    id: str
    title: str
    task: str
    repo: str | None
    status: StatusView
    is_running: bool
    error: str | None
    started: str
    duration: str
    cost: str
    llm_calls: int
    iterations: int
    replans: int
    tests: StatusView | None
    cache_share: str
    cost_chart: BarChart | None
    token_chart: BarChart | None
    table_columns: tuple[str, ...]
    table_rows: tuple[TableRow, ...]
    steps: tuple[StepView, ...]
    diff_html: Markup | None
    failure_report: str | None
    pr_url: str | None
    stages: tuple[StageView, ...]
    activity: tuple[ActivityView, ...]


def _step_view(row: Row) -> StepView:
    tokens = sum(
        row[column]
        for column in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
    )
    return StepView(
        id=row["id"],
        agent=row["agent"],
        iteration=row["iteration"],
        status=status_view(row["status"]),
        is_running=row["status"] == "running",
        summary=row["summary"],
        has_details=row["llm_calls"] > 0,
        llm_calls=row["llm_calls"],
        cost=format_usd(row["cost_usd"]),
        tokens=format_tokens(tokens),
        duration=format_duration(row["started_at"], row["finished_at"]),
    )


def _read_artifact(work_root: Path, run_id: str, name: str) -> str | None:
    # run_id always comes from a database row, so this cannot be steered elsewhere.
    path = work_root / run_id / name
    return path.read_text(errors="replace") if path.is_file() else None


def _highlight_diff(diff: str) -> Markup:
    # Pygments escapes its input, so diff text from a repository can't inject markup.
    formatter = HtmlFormatter(nowrap=True)
    return Markup(highlight(clip(diff, _MAX_DIFF_CHARS, 0), DiffLexer(), formatter))


def run_view(store: TraceStore, run_id: str, work_root: Path) -> RunView:
    run = store.get_run(run_id)
    rows = store.steps(run_id)
    usage = store.usage_by_agent(run_id)

    cost_chart = horizontal_bars(
        "Cost per agent",
        _COST_SERIES,
        [Datum(row["agent"], {"cost_usd": row["cost_usd"]}) for row in usage],
        format_usd,
    )
    token_chart = horizontal_bars(
        "Tokens per agent",
        _TOKEN_SERIES,
        [Datum(row["agent"], {item.key: row[item.key] for item in _TOKEN_SERIES}) for row in usage],
        format_tokens,
    )
    table_rows = tuple(
        TableRow(
            row["agent"],
            (
                format_usd(row["cost_usd"]),
                *(f"{row[item.key]:,}" for item in _TOKEN_SERIES),
            ),
        )
        for row in usage
    )

    verifications = [row for row in rows if row["agent"] == "verify"]
    prompt_tokens = run["input_tokens"] + run["cache_read_tokens"] + run["cache_write_tokens"]
    diff = _read_artifact(work_root, run_id, "changes.diff")
    pr_url = _read_artifact(work_root, run_id, "pr_url.txt")
    return RunView(
        id=run["id"],
        title=_first_line(run["task"]),
        task=run["task"],
        repo=run["repo"],
        status=status_view(run["status"]),
        is_running=run["status"] == "running",
        error=run["error"],
        started=format_time(run["created_at"]),
        duration=format_duration(run["created_at"], run["finished_at"]),
        cost=format_usd(run["cost_usd"]),
        llm_calls=run["llm_calls"],
        iterations=max((row["iteration"] for row in rows), default=0),
        replans=max(sum(row["agent"] == "planner" for row in rows) - 1, 0),
        tests=status_view(verifications[-1]["status"]) if verifications else None,
        cache_share=f"{run['cache_read_tokens'] / prompt_tokens:.0%}" if prompt_tokens else "-",
        cost_chart=cost_chart,
        token_chart=token_chart,
        table_columns=("Cost", *(item.label for item in _TOKEN_SERIES)),
        table_rows=table_rows,
        steps=tuple(_step_view(row) for row in rows),
        diff_html=_highlight_diff(diff) if diff else None,
        failure_report=_read_artifact(work_root, run_id, "failure_report.md"),
        pr_url=pr_url.strip() if pr_url else None,
        stages=stage_views(rows, run["status"]),
        activity=activity_views(store, run_id),
    )


@dataclass(frozen=True)
class Block:
    label: str
    text: str
    is_error: bool = False


@dataclass(frozen=True)
class ToolCallView:
    name: str
    input: str
    output: str
    is_error: bool
    latency_ms: int


@dataclass(frozen=True)
class TurnView:
    number: int
    model: str
    effort: str
    stop_reason: str | None
    error: str | None
    cost: str
    latency_ms: int
    usage: str
    retries: int
    system_prompt: str | None
    prompts: tuple[Block, ...]
    responses: tuple[Block, ...]
    tool_calls: tuple[ToolCallView, ...]


def _prompt_blocks(messages: list[Any]) -> tuple[Block, ...]:
    """What was newly said to the model. Replies and tool results are shown elsewhere."""
    blocks = []
    for message in messages:
        if message["role"] != "user":
            continue
        content = message["content"]
        if isinstance(content, str):
            blocks.append(Block("Message", content))
            continue
        blocks += [Block("Reminder", part["text"]) for part in content if part["type"] == "text"]
    return tuple(blocks)


def _response_blocks(content: list[Any]) -> tuple[Block, ...]:
    blocks = []
    for part in content:
        if part["type"] == "text":
            blocks.append(Block("Text", part["text"]))
        elif part["type"] == "thinking" and part.get("thinking"):
            blocks.append(Block("Thinking summary", part["thinking"]))
    return tuple(blocks)


def step_turns(store: TraceStore, step_id: int) -> list[TurnView]:
    tools_by_call: dict[int, list[ToolCallView]] = {}
    for row in store.tool_calls(step_id):
        tools_by_call.setdefault(row["llm_call_id"], []).append(
            ToolCallView(
                name=row["tool_name"],
                input=json.dumps(json.loads(row["input_json"]), indent=2),
                output=row["output"],
                is_error=bool(row["is_error"]),
                latency_ms=row["latency_ms"],
            )
        )

    turns = []
    for number, call in enumerate(store.llm_calls(step_id), start=1):
        response = json.loads(call["response_json"]) if call["response_json"] else []
        turns.append(
            TurnView(
                number=number,
                model=call["model"],
                effort=call["effort"],
                stop_reason=call["stop_reason"],
                error=call["error"],
                cost=format_usd(call["cost_usd"]),
                latency_ms=call["latency_ms"],
                usage=(
                    f"{call['input_tokens']:,} in, {call['output_tokens']:,} out, "
                    f"{call['cache_read_tokens']:,} cache read, "
                    f"{call['cache_write_tokens']:,} cache write"
                ),
                retries=len(json.loads(call["retries_json"])),
                system_prompt=call["system_prompt"] if number == 1 else None,
                prompts=_prompt_blocks(json.loads(call["new_messages_json"])),
                responses=_response_blocks(response),
                tool_calls=tuple(tools_by_call.get(call["id"], ())),
            )
        )
    return turns


_STAGES = (
    ("Plan", {"planner"}),
    ("Code", {"coder"}),
    ("Test", {"tester", "verify"}),
    ("Review", {"reviewer"}),
    ("Finish", set[str]()),
)
_STAGE_ICONS = {"done": "✓", "active": "●", "failed": "✕", "pending": "○"}
_ACTIVITY_ITEMS = 10
_ACTIVITY_DETAIL_CHARS = 100
_DETAIL_FIELDS = ("path", "cmd", "pattern")
_ISSUE_REPO = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)$")


def stage_views(rows: list[Row], status: str) -> tuple[StageView, ...]:
    latest = rows[-1]["agent"] if rows else None
    current = next((i for i, (_, agents) in enumerate(_STAGES) if latest in agents), -1)

    def state(index: int) -> str:
        if status == "done" or index < current:
            return "done"
        if index == current:
            return "active" if status == "running" else "failed"
        return "pending"

    return tuple(
        StageView(label, state(index), _STAGE_ICONS[state(index)])
        for index, (label, _) in enumerate(_STAGES)
    )


def activity_views(store: TraceStore, run_id: str) -> tuple[ActivityView, ...]:
    items = []
    for row in store.recent_tool_calls(run_id, _ACTIVITY_ITEMS):
        arguments = json.loads(row["input_json"])
        detail = next((str(arguments[key]) for key in _DETAIL_FIELDS if key in arguments), "")
        if len(detail) > _ACTIVITY_DETAIL_CHARS:
            detail = detail[:_ACTIVITY_DETAIL_CHARS] + "..."
        items.append(ActivityView(row["agent"], row["tool_name"], detail, bool(row["is_error"])))
    return tuple(items)


@dataclass(frozen=True)
class HistoryItem:
    run_id: str
    issue_ref: str
    issue_url: str
    title: str
    status: StatusView
    pr_url: str | None
    pr_label: str | None
    cost: str
    cost_usd: float
    started: str


def history(store: TraceStore, work_root: Path) -> list[HistoryItem]:
    """Runs that came from a GitHub issue, newest first."""
    items = []
    for row in store.list_runs():
        match = _ISSUE_REPO.match(row["repo"] or "")
        if match is None:
            continue
        pr_url = _read_artifact(work_root, row["id"], "pr_url.txt")
        pr_url = pr_url.strip() if pr_url else None
        items.append(
            HistoryItem(
                run_id=row["id"],
                issue_ref=row["repo"],
                issue_url=f"https://github.com/{match[1]}/issues/{match[2]}",
                title=_first_line(row["task"]),
                status=status_view(row["status"]),
                pr_url=pr_url,
                pr_label=f"#{pr_url.rsplit('/', 1)[-1]}" if pr_url else None,
                cost=format_usd(row["cost_usd"]),
                cost_usd=row["cost_usd"],
                started=format_time(row["created_at"]),
            )
        )
    return items


@dataclass(frozen=True)
class HistoryStats:
    pull_requests: int
    attempted: int
    success_rate: str
    average_cost: str
    total_cost: str


def history_stats(items: list[HistoryItem]) -> HistoryStats:
    finished = [item for item in items if item.status.kind != "running"]
    succeeded = sum(item.status.kind == "good" for item in finished)
    total = sum(item.cost_usd for item in items)
    return HistoryStats(
        pull_requests=sum(item.pr_url is not None for item in items),
        attempted=len(items),
        success_rate=f"{succeeded / len(finished):.0%}" if finished else "-",
        average_cost=format_usd(total / len(items)) if items else "-",
        total_cost=format_usd(total),
    )


@dataclass(frozen=True)
class JobView:
    id: str
    ref: str
    issue_url: str
    status: StatusView
    is_active: bool
    note: str
    error: str | None
    pr_url: str | None
    run: RunView | None


def _job_note(job: Job, position: int) -> str:
    if job.status == "running":
        return "The agents are working on this now." if job.run_id else "Checking the repository."
    if job.status == "queued":
        if position <= 1:
            return "Next in line. Starting in a moment."
        return f"Waiting for {position - 1} job(s) ahead of this one."
    return ""


def _job_status(job: Job) -> StatusView:
    return (
        StatusView("Queued", "running", "○") if job.status == "queued" else status_view(job.status)
    )


def job_view(job: Job, queue: JobQueue, store: TraceStore | None, work_root: Path) -> JobView:
    match = _ISSUE_REPO.match(job.issue_ref)
    assert match is not None  # Only validated references are ever queued.
    return JobView(
        id=job.id,
        ref=job.issue_ref,
        issue_url=f"https://github.com/{match[1]}/issues/{match[2]}",
        status=_job_status(job),
        is_active=job.is_active,
        note=_job_note(job, queue.position(job)),
        error=job.error,
        pr_url=job.pr_url,
        run=run_view(store, job.run_id, work_root) if store and job.run_id else None,
    )


def active_jobs(queue: JobQueue) -> list[JobView]:
    return [job_view(job, queue, None, Path()) for job in queue.active()]
