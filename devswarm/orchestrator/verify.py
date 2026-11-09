"""Run the project's test suite and turn the outcome into a verdict.

The exit code decides, never an agent's claim.
"""

from __future__ import annotations

from dataclasses import dataclass

from devswarm.sandbox.base import Sandbox
from devswarm.sandbox.images import LanguageProfile

_TEST_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class TestReport:
    passed: bool
    exit_code: int
    output: str
    fingerprints: tuple[str, ...] = ()


class SandboxTestRunner:
    def __init__(self, sandbox: Sandbox, profile: LanguageProfile) -> None:
        self._sandbox = sandbox
        self._profile = profile

    def run(self) -> TestReport:
        result = self._sandbox.exec(self._profile.test_command(), timeout=_TEST_TIMEOUT_SECONDS)
        output = result.stdout + result.stderr
        if result.exit_code == 0:
            return TestReport(True, 0, output)
        if result.timed_out:
            fingerprints: tuple[str, ...] = ("timeout",)
        else:
            parsed = self._profile.failure_fingerprints(output)
            fingerprints = tuple(parsed) or (f"exit:{result.exit_code}",)
        return TestReport(False, result.exit_code, output, fingerprints)
