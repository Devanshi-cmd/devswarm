import pytest
from anthropic.types import MessageParam

from devswarm.llm.mock import (
    MockLLMClient,
    MockScriptExhausted,
    refusal_turn,
    text_turn,
    tool_turn,
)
from devswarm.llm.types import LLMError, LLMRequest

LONG_SYSTEM = "stable instructions " * 400


def request(messages: list[MessageParam], system: str = LONG_SYSTEM) -> LLMRequest:
    return LLMRequest(model="claude-haiku-5-5", system=system, messages=messages)


def test_replays_script_in_order() -> None:
    client = MockLLMClient([tool_turn("read_file", {"path": "a.py"}), text_turn("done")])
    first = client.create(request([{"role": "user", "content": "go"}]))
    assert first.stop_reason == "tool_use"
    assert [block.type for block in first.content] == ["thinking", "tool_use"]


def test_refusal_turn() -> None:
    response = MockLLMClient([refusal_turn("bio")]).create(request([]))
    assert response.stop_reason == "refusal"
    assert response.refusal_category == "bio"


def test_exhausted_script_fails_loudly() -> None:
    with pytest.raises(MockScriptExhausted):
        MockLLMClient().create(request([]))


def test_scripted_exception_is_raised() -> None:
    with pytest.raises(LLMError):
        MockLLMClient([LLMError("rate limited", status_code=429)]).create(request([]))


def test_requests_are_snapshots() -> None:
    client = MockLLMClient([text_turn("ok")])
    messages: list[MessageParam] = [{"role": "user", "content": "go"}]
    client.create(request(messages))
    messages.append({"role": "assistant", "content": "later"})
    assert len(client.requests[0].messages) == 1


def test_second_turn_reads_from_cache() -> None:
    client = MockLLMClient([text_turn("a"), text_turn("b")])
    messages: list[MessageParam] = [{"role": "user", "content": "one"}]
    first = client.create(request(messages))
    messages += [
        {"role": "assistant", "content": first.content},
        {"role": "user", "content": "two"},
    ]
    second = client.create(request(messages))

    assert first.usage.cache_read_input_tokens == 0
    assert first.usage.cache_creation_input_tokens > 0
    assert second.usage.cache_read_input_tokens == first.usage.prompt_tokens


def test_small_prompts_are_not_cached() -> None:
    usage = (
        MockLLMClient([text_turn("a")])
        .create(request([{"role": "user", "content": "hi"}], system="short"))
        .usage
    )
    assert usage.cache_creation_input_tokens == 0
    assert usage.input_tokens > 0


def test_editing_history_is_rejected_like_the_real_api() -> None:
    client = MockLLMClient([text_turn("a"), text_turn("b")])
    first_message: MessageParam = {"role": "user", "content": "one"}
    tool_output: MessageParam = {"role": "user", "content": "long tool output"}
    client.create(request([first_message, tool_output]))

    edited: list[MessageParam] = [
        first_message,
        {"role": "user", "content": "pruned"},
        {"role": "user", "content": "next"},
    ]
    with pytest.raises(LLMError, match="edited") as caught:
        client.create(request(edited))
    assert caught.value.status_code == 400
