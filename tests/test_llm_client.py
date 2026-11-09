"""The real client against a fake HTTP transport: real SDK code, no network."""

import json
import logging
from collections.abc import Callable

import anthropic
import httpx2
import pytest

from devswarm.llm.client import AnthropicLLMClient, RetryPolicy
from devswarm.llm.types import LLMError, LLMRequest

REQUEST = LLMRequest(
    model="claude-haiku-5-5",
    system="You are a test.",
    messages=[{"role": "user", "content": "hi"}],
    effort="high",
)


def success_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "cache_read_input_tokens": 2000,
            "cache_creation_input_tokens": 400,
        },
    }
    body.update(overrides)
    return body


def error_response(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        status,
        headers=headers,
        json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
    )


class Harness:
    def __init__(self, responses: list[httpx2.Response]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []
        self.sleeps: list[float] = []
        handler: Callable[[httpx2.Request], httpx2.Response] = self._handle
        sdk = anthropic.Anthropic(
            api_key="test-key", http_client=httpx2.Client(transport=httpx2.MockTransport(handler))
        )
        self.client = AnthropicLLMClient(
            sdk, policy=RetryPolicy(max_retries=3), sleep=self.sleeps.append, rng=lambda: 1.0
        )

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if response.status_code == 599:
            raise httpx2.ConnectError("connection refused", request=request)
        return response


def test_request_shape_matches_design() -> None:
    harness = Harness([httpx2.Response(200, json=success_body())])
    harness.client.create(REQUEST)

    body = json.loads(harness.requests[0].content)
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "high"}
    assert "tools" not in body
    assert "tool_choice" not in body
    assert "temperature" not in body


def test_response_is_translated_with_cost() -> None:
    harness = Harness([httpx2.Response(200, json=success_body())])
    response = harness.client.create(REQUEST)

    assert response.stop_reason == "end_turn"
    assert response.usage.cache_read_input_tokens == 2000
    # 1000*0.10 + 100*0.50 + 2000*0.01 + 400*0.125, per million tokens.
    assert response.cost_usd == pytest.approx(220 / 1_000_000)
    assert response.retries == ()


def test_refusal_category_is_exposed() -> None:
    body = success_body(
        content=[],
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": "no"},
    )
    response = Harness([httpx2.Response(200, json=body)]).client.create(REQUEST)
    assert response.stop_reason == "refusal"
    assert response.refusal_category == "cyber"


@pytest.mark.parametrize("status", [429, 500, 503, 529])
def test_retries_rate_limits_and_server_errors(status: int) -> None:
    harness = Harness([error_response(status), httpx2.Response(200, json=success_body())])
    response = harness.client.create(REQUEST)

    assert len(harness.requests) == 2
    assert [retry.reason for retry in response.retries] == [f"HTTP {status}"]


def test_backoff_doubles_each_attempt() -> None:
    harness = Harness(
        [error_response(500), error_response(500), httpx2.Response(200, json=success_body())]
    )
    harness.client.create(REQUEST)
    assert harness.sleeps == [1.0, 2.0]


def test_retry_after_header_is_honoured() -> None:
    harness = Harness(
        [
            error_response(429, headers={"retry-after": "7"}),
            httpx2.Response(200, json=success_body()),
        ]
    )
    harness.client.create(REQUEST)
    assert harness.sleeps == [7.0]


def test_connection_errors_are_retried() -> None:
    harness = Harness([httpx2.Response(599), httpx2.Response(200, json=success_body())])
    response = harness.client.create(REQUEST)
    assert len(response.retries) == 1
    assert "Connection" in response.retries[0].reason


@pytest.mark.parametrize("status", [400, 401, 404])
def test_client_errors_are_not_retried(status: int) -> None:
    harness = Harness([error_response(status)])
    with pytest.raises(LLMError) as caught:
        harness.client.create(REQUEST)
    assert caught.value.status_code == status
    assert len(harness.requests) == 1
    assert harness.sleeps == []


def test_gives_up_after_max_retries() -> None:
    harness = Harness([error_response(500) for _ in range(4)])
    with pytest.raises(LLMError, match="giving up after 4 attempts") as caught:
        harness.client.create(REQUEST)
    assert len(harness.requests) == 4
    assert len(caught.value.retries) == 3


def test_sdk_retries_are_disabled() -> None:
    # If the SDK also retried, one scripted failure would be consumed invisibly.
    harness = Harness([error_response(500), httpx2.Response(200, json=success_body())])
    assert len(harness.client.create(REQUEST).retries) == 1


def test_warns_when_near_long_context(caplog: pytest.LogCaptureFixture) -> None:
    body = success_body(
        usage={"input_tokens": 80_000, "output_tokens": 1, "cache_read_input_tokens": 0}
    )
    with caplog.at_level(logging.WARNING):
        Harness([httpx2.Response(200, json=body)]).client.create(REQUEST)
    assert "close to the 100000 long-context threshold" in caplog.text


def test_cost_uses_the_requested_model_when_the_api_echoes_a_variant() -> None:
    body = success_body(model="claude-haiku-5-5-20990101")
    response = Harness([httpx2.Response(200, json=body)]).client.create(REQUEST)
    assert response.cost_usd == pytest.approx(220 / 1_000_000)
