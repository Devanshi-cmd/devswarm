"""Run the project's test suite and turn the outcome into a verdict.

The exit code decides, never an agent's claim.
"""

from __future__ import annotations

from dataclasses import dataclass

from devswarm.sandbox.base import Sandbox
from devswarm.sandbox.images import LanguageProfile

# Only failures with a specific identity can be matched against the starting point.
# A build that doesn't compile, or a suite that hangs, never counts as already-known.
_UNMATCHABLE_PREFIXES = ("exit:", "compile|", "timeout")


@dataclass(frozen=True)
class TestReport:
    passed: bool
    exit_code: int
    output: str
    fingerprints: tuple[str, ...] = ()
    known_failures: tuple[str, ...] = ()


class SandboxTestRunner:
    def __init__(self, sandbox: Sandbox, profile: LanguageProfile) -> None:
        self._sandbox = sandbox
        self._profile = profile
        self._baseline: frozenset[str] = frozenset()

    @property
    def known_failures(self) -> tuple[str, ...]:
        return tuple(sorted(self._baseline))

    def record_baseline(self) -> TestReport:
        """Run the suite before any change, to learn which tests already fail.

        Real projects often have a few failing tests. Without this, one of those would
        make every fix look broken, however correct it was.
        """
        report = self.run()
        if not report.passed:
            self._baseline = frozenset(
                item for item in report.fingerprints if not item.startswith(_UNMATCHABLE_PREFIXES)
            )
        return report

    def run(self) -> TestReport:
        result = self._sandbox.exec(
            self._profile.test_command(), timeout=self._profile.test_timeout
        )
        output = result.stdout + result.stderr
        if result.exit_code == 0:
            return TestReport(True, 0, output)
        if result.timed_out:
            fingerprints: tuple[str, ...] = ("timeout",)
        else:
            parsed = self._profile.failure_fingerprints(output)
            fingerprints = tuple(parsed) or (f"exit:{result.exit_code}",)

        if self._baseline and set(fingerprints) <= self._baseline:
            note = "Every failure below was already failing before this change.\n\n"
            return TestReport(True, result.exit_code, note + output, (), fingerprints)
        return TestReport(False, result.exit_code, output, fingerprints)
