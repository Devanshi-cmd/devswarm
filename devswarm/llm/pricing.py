"""Per-model prices and cost calculation.

Prices are USD per million tokens, taken from Anthropic's docs (snapshot 2026-10-06).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Warn once a prompt reaches this share of a model's long-context threshold.
LONG_CONTEXT_WARN_RATIO = 0.7


class UnknownModelError(KeyError):
    """Raised for a model with no price entry, so cost is never silently recorded as zero."""


@dataclass(frozen=True)
class Rates:
    input: float
    output: float
    cache_read: float
    cache_write_5m: float


@dataclass(frozen=True)
class ModelPricing:
    base: Rates
    long_context: Rates | None = None
    long_context_threshold: int | None = None

    def rates_for(self, prompt_tokens: int) -> Rates:
        if (
            self.long_context is not None
            and self.long_context_threshold is not None
            and prompt_tokens > self.long_context_threshold
        ):
            return self.long_context
        return self.base


# Cache read prices are listed per model because they are not a fixed share of input:
# 0.1x on Haiku and Sonnet 5.5, but 0.05x on Opus 5.5.
PRICE_TABLE: dict[str, ModelPricing] = {
    "claude-haiku-5-5": ModelPricing(
        base=Rates(input=0.10, output=0.50, cache_read=0.01, cache_write_5m=0.125),
        long_context=Rates(input=0.50, output=2.50, cache_read=0.05, cache_write_5m=0.625),
        long_context_threshold=100_000,
    ),
    "claude-sonnet-5-5": ModelPricing(
        base=Rates(input=2.00, output=10.00, cache_read=0.20, cache_write_5m=2.50),
    ),
    "claude-opus-5-5": ModelPricing(
        base=Rates(input=4.00, output=20.00, cache_read=0.20, cache_write_5m=5.00),
    ),
}


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        # The API reports cached and uncached input separately. We count all three toward
        # the long-context threshold, the cautious reading of "prompt length".
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens


def pricing_for(model: str) -> ModelPricing:
    try:
        return PRICE_TABLE[model]
    except KeyError:
        raise UnknownModelError(model) from None


def calculate_cost(model: str, usage: TokenUsage) -> float:
    rates = pricing_for(model).rates_for(usage.prompt_tokens)
    return (
        usage.input_tokens * rates.input
        + usage.output_tokens * rates.output
        + usage.cache_read_input_tokens * rates.cache_read
        + usage.cache_creation_input_tokens * rates.cache_write_5m
    ) / 1_000_000


def warn_if_near_long_context(model: str, usage: TokenUsage) -> None:
    threshold = pricing_for(model).long_context_threshold
    if threshold is None:
        return
    prompt = usage.prompt_tokens
    if prompt > threshold:
        logger.warning(
            "%s prompt was %d tokens, over the %d threshold: billed at the long-context rate",
            model,
            prompt,
            threshold,
        )
    elif prompt >= threshold * LONG_CONTEXT_WARN_RATIO:
        logger.warning(
            "%s prompt is %d tokens, close to the %d long-context threshold",
            model,
            prompt,
            threshold,
        )
