"""Test doubles shared across the suite."""

from collections.abc import Sequence

from devswarm.sandbox.base import CommandResult


def result(
    exit_code: int = 0, stdout: str = "", stderr: str = "", timed_out: bool = False
) -> CommandResult:
    return CommandResult(exit_code, stdout, stderr, timed_out, duration_ms=1)


class FakeSandbox:
    def __init__(self, *results: CommandResult) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []
        self.timeouts: list[int] = []
        self.head_chars: list[int] = []

    def exec(
        self,
        argv: Sequence[str],
        *,
        timeout: int = 120,
        head_chars: int = 2_000,
        tail_chars: int = 8_000,
    ) -> CommandResult:
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        self.head_chars.append(head_chars)
        return self.results.pop(0) if self.results else result()
