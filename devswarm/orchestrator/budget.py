"""Per-run spending limits, shared by every agent in the run."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from devswarm.llm.pricing import TokenUsage


@dataclass(frozen=True)
class BudgetLimits:
    max_usd: float = 1.00
    # Cache reads are excluded: they dominate long loops, cost little, and max_usd covers them.
    max_tokens: int = 1_000_000
    max_seconds: float = 1_200


class Budget:
    def __init__(
        self,
        limits: BudgetLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits or BudgetLimits()
        self._clock = clock
        self._started = clock()
        self.spent_usd = 0.0
        self.spent_tokens = 0

    def charge(self, usage: TokenUsage, cost_usd: float) -> None:
        self.spent_usd += cost_usd
        self.spent_tokens += (
            usage.input_tokens + usage.output_tokens + usage.cache_creation_input_tokens
        )

    def exceeded(self) -> str | None:
        """Explain which limit has been reached, or return None if work may continue."""
        limits = self.limits
        if self.spent_usd >= limits.max_usd:
            return f"cost budget reached: ${self.spent_usd:.4f} of ${limits.max_usd:.2f}"
        if self.spent_tokens >= limits.max_tokens:
            return f"token budget reached: {self.spent_tokens} of {limits.max_tokens}"
        elapsed = self._clock() - self._started
        if elapsed >= limits.max_seconds:
            return f"time budget reached: {elapsed:.0f}s of {limits.max_seconds:.0f}s"
        return None
