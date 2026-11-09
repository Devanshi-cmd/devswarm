"""Turn stored trace rows into the plain values the templates display."""

from __future__ import annotations

import json
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
