import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from anthropic.types import MessageParam

from devswarm.llm.mock import MockLLMClient, text_turn, tool_turn
from devswarm.llm.pricing import TokenUsage
from devswarm.llm.types import LLMError, LLMRequest, LLMResponse, RetryRecord
from devswarm.storage.redact import REPLACEMENT, Redactor
from devswarm.storage.report import render_markdown
from devswarm.storage.trace import RunNotFoundError, TraceStore

SECRET = "topsecretvalue"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TraceStore]:
    with TraceStore(tmp_path / "nested" / "trace.sqlite3", Redactor([SECRET])) as trace:
        yield trace


def make_request(messages: list[MessageParam]) -> LLMRequest:
    return LLMRequest(model="claude-haiku-5-5", system=f"sys {SECRET}", messages=messages)


def make_response(cost: float = 0.5) -> LLMResponse:
    return LLMResponse(
        id="msg",
        model="claude-haiku-5-5",
        content=text_turn("ok").content,
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        cost_usd=cost,
        latency_ms=12,
        retries=(RetryRecord(attempt=1, reason="HTTP 429", delay_seconds=1.5),),
    )


def test_run_lifecycle(store: TraceStore) -> None:
    run_id = store.start_run("fix bug", repo="./r", config={"model": "x"})
    assert store.get_run(run_id)["status"] == "running"
    store.finish_run(run_id, "failed", error="ran out")

    run = store.get_run(run_id)
    assert (run["status"], run["error"]) == ("failed", "ran out")
    assert run["finished_at"] is not None


def test_unknown_run_raises(store: TraceStore) -> None:
    with pytest.raises(RunNotFoundError):
        store.get_run("nope")


def test_usage_rolls_up_to_step_and_run(store: TraceStore) -> None:
    run_id = store.start_run("t")
    first, second = store.start_step(run_id, "coder"), store.start_step(run_id, "tester")
    store.record_llm_call(first, make_request([]), make_response(0.25))
    store.record_llm_call(first, make_request([]), make_response(0.25))
    store.record_llm_call(second, make_request([]), make_response(1.0))

    run = store.get_run(run_id)
    assert (run["llm_calls"], run["input_tokens"]) == (3, 30)
    assert run["cost_usd"] == pytest.approx(1.5)
    assert [step["llm_calls"] for step in store.steps(run_id)] == [2, 1]


def test_tool_call_is_stored(store: TraceStore) -> None:
    run_id = store.start_run("t")
    step = store.start_step(run_id, "coder")
    llm_call = store.record_llm_call(step, make_request([]), make_response())
    store.record_tool_call(
        step,
        llm_call,
        tool_name="read_file",
        tool_input={"path": "a.py"},
        output="contents",
        is_error=False,
        latency_ms=3,
    )
    row = store.tool_calls(step)[0]
    assert (row["tool_name"], row["output"], row["is_error"]) == ("read_file", "contents", 0)
    assert json.loads(row["input_json"]) == {"path": "a.py"}


def test_retries_are_recorded(store: TraceStore) -> None:
    run_id = store.start_run("t")
    step = store.start_step(run_id, "coder")
    store.record_llm_call(step, make_request([]), make_response())
    assert json.loads(store.llm_calls(step)[0]["retries_json"])[0]["reason"] == "HTTP 429"


def test_failed_call_is_recorded(store: TraceStore) -> None:
    run_id = store.start_run("t")
    step = store.start_step(run_id, "coder")
    store.record_llm_error(step, make_request([]), LLMError("HTTP 400: nope", status_code=400))
    row = store.llm_calls(step)[0]
    assert row["error"] == "HTTP 400: nope"
    assert (row["response_json"], row["cost_usd"]) == (None, 0)


def test_only_new_messages_are_stored_and_full_list_is_rebuilt(store: TraceStore) -> None:
    run_id = store.start_run("t")
    step = store.start_step(run_id, "coder")
    client = MockLLMClient([tool_turn("read_file", {"path": "a.py"}), text_turn("done")])

    messages: list[MessageParam] = [{"role": "user", "content": "go"}]
    request = make_request(list(messages))
    response = client.create(request)
    store.record_llm_call(step, request, response)
    messages += [
        {"role": "assistant", "content": response.content},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "code"}],
        },
    ]
    request = make_request(list(messages))
    store.record_llm_call(step, request, client.create(request))

    deltas = [json.loads(call["new_messages_json"]) for call in store.llm_calls(step)]
    assert [len(delta) for delta in deltas] == [1, 2]
    rebuilt = store.reconstruct_messages(step)
    assert len(rebuilt) == 3
    assert rebuilt[1]["content"][1]["name"] == "read_file"


def test_secrets_never_reach_the_database(store: TraceStore, tmp_path: Path) -> None:
    run_id = store.start_run(f"task {SECRET}", repo=SECRET, config={"k": SECRET})
    step = store.start_step(run_id, "coder")
    request = make_request([{"role": "user", "content": f"message {SECRET}"}])
    response = make_response()
    response.content[0].text = f"reply {SECRET}"  # type: ignore[union-attr]
    llm_call = store.record_llm_call(step, request, response)
    store.record_llm_error(step, request, LLMError(f"failed {SECRET}"))
    store.record_tool_call(
        step,
        llm_call,
        tool_name="run_command",
        tool_input={"cmd": f"echo {SECRET}"},
        output=f"out {SECRET}",
        is_error=False,
        latency_ms=1,
    )
    store.finish_step(step, "done", summary=f"summary {SECRET}")
    store.finish_run(run_id, "failed", error=f"error {SECRET}")
    store.close()

    raw = b"".join(path.read_bytes() for path in (tmp_path / "nested").iterdir())
    assert SECRET.encode() not in raw
    assert REPLACEMENT.encode() in raw


def test_markdown_report(store: TraceStore) -> None:
    run_id = store.start_run("fix the bug", repo="./r")
    step = store.start_step(run_id, "coder", iteration=2)
    store.record_llm_call(step, make_request([]), make_response(0.125))
    store.finish_step(step, "done")
    store.finish_run(run_id, "done")

    report = render_markdown(store, run_id)
    assert "fix the bug" in report
    assert "$0.125000" in report
    assert "| coder | 2 | done | 1 | 10 | 5 |" in report
