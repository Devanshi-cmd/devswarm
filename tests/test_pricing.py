import logging

import pytest

from devswarm.llm.pricing import (
    TokenUsage,
    UnknownModelError,
    calculate_cost,
    warn_if_near_long_context,
)

HAIKU = "claude-haiku-5-5"


def test_haiku_base_rates() -> None:
    usage = TokenUsage(input_tokens=100_000, output_tokens=1_000_000)
    assert calculate_cost(HAIKU, usage) == pytest.approx(0.01 + 0.50)


def test_haiku_cache_rates() -> None:
    usage = TokenUsage(cache_read_input_tokens=50_000, cache_creation_input_tokens=50_000)
    assert calculate_cost(HAIKU, usage) == pytest.approx(0.0005 + 0.00625)


def test_exactly_at_threshold_stays_on_cheap_rate() -> None:
    assert calculate_cost(HAIKU, TokenUsage(input_tokens=100_000)) == pytest.approx(0.01)


def test_over_threshold_reprices_the_whole_request() -> None:
    usage = TokenUsage(input_tokens=100_001, output_tokens=1_000_000)
    assert calculate_cost(HAIKU, usage) == pytest.approx(100_001 * 0.5 / 1e6 + 2.50)


def test_cached_tokens_count_toward_threshold() -> None:
    usage = TokenUsage(input_tokens=1, cache_read_input_tokens=100_000)
    assert calculate_cost(HAIKU, usage) == pytest.approx((0.5 + 100_000 * 0.05) / 1e6)


def test_opus_cache_read_is_not_a_tenth_of_input() -> None:
    usage = TokenUsage(cache_read_input_tokens=1_000_000)
    assert calculate_cost("claude-opus-5-5", usage) == pytest.approx(0.20)


def test_unknown_model_raises() -> None:
    with pytest.raises(UnknownModelError):
        calculate_cost("claude-imaginary", TokenUsage(input_tokens=1))


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [(69_999, None), (70_000, "close to"), (100_000, "close to"), (100_001, "long-context rate")],
)
def test_long_context_warning(
    tokens: int, expected: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        warn_if_near_long_context(HAIKU, TokenUsage(input_tokens=tokens))
    if expected is None:
        assert not caplog.records
    else:
        assert expected in caplog.text


def test_models_without_long_context_never_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        warn_if_near_long_context("claude-sonnet-5-5", TokenUsage(input_tokens=500_000))
    assert not caplog.records
