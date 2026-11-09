"""The orchestrator: runs one task through the state machine.

Agents, the repository and the test runner are injected, so all of this logic can be
tested with fakes. Agents never see each other's transcripts, only the summaries below.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

from devswarm.agents.base import AgentContext, AgentResult
from devswarm.agents.coder import CoderOutput
from devswarm.agents.planner import PlanOutput, format_plan
from devswarm.agents.prompting import Section
from devswarm.agents.reviewer import ReviewOutput
from devswarm.agents.tester import TesterOutput
from devswarm.orchestrator.budget import Budget
from devswarm.orchestrator.loops import LoopDetector, without_paths
from devswarm.orchestrator.state_machine import (
    TERMINAL_STATES,
    Counters,
    Event,
    MachineConfig,
    State,
    transition,
)
from devswarm.orchestrator.verify import TestReport
from devswarm.sandbox.output import clip
from devswarm.storage.trace import TraceStore

_DIFF_HEAD_CHARS = 30_000
_DIFF_TAIL_CHARS = 10_000
_TEST_HEAD_CHARS = 2_000
_TEST_TAIL_CHARS = 8_000
_SUMMARY_TAIL_CHARS = 4_000


class AgentRunner[OutputT: BaseModel](Protocol):
    def run(self, task: str, context: AgentContext) -> AgentResult[OutputT]: ...


class RepoState(Protocol):
    def snapshot(self) -> str: ...
    def changed_paths(self, before: str, after: str) -> list[str]: ...
    def restore(self, snapshot: str, paths: list[str]) -> None: ...
    def diff(self) -> str: ...
    def reset_to_base(self) -> None: ...
    def is_test_path(self, path: str) -> bool: ...


class SuiteRunner(Protocol):
    def run(self) -> TestReport: ...


class FinalizeError(Exception):
    """The finished change could not be saved or published."""


@dataclass(frozen=True)
class Agents:
    planner: AgentRunner[PlanOutput]
    coder: AgentRunner[CoderOutput]
    tester: AgentRunner[TesterOutput]
    reviewer: AgentRunner[ReviewOutput]


@dataclass(frozen=True)
class RunSummary:
    task: str
    plan: PlanOutput | None
    coder_summary: str
    review: ReviewOutput | None
    tests: TestReport | None
    iterations: int
    replans: int


Finalizer = Callable[[RunSummary], None]


@dataclass(frozen=True)
class OrchestratorResult:
    status: State
    summary: RunSummary
    reason: str | None = None
    failure_report: str | None = None


@dataclass
class _Memory:
    plan: PlanOutput | None = None
    coder_summary: str = ""
    tests: TestReport | None = None
    review: ReviewOutput | None = None
    failing_output: str = ""
    feedback: list[str] = field(default_factory=list)
    no_changes_note: str = ""
    coder_test_edits: set[str] = field(default_factory=set)
    history: list[str] = field(default_factory=list)
    failure_reason: str | None = None


class Orchestrator:
    """Handles one run. Create a new instance for each task."""

    def __init__(
        self,
        *,
        run_id: str,
        agents: Agents,
        repo: RepoState,
        tests: SuiteRunner,
        finalize: Finalizer,
        store: TraceStore,
        budget: Budget,
        config: MachineConfig | None = None,
        on_progress: Callable[[str], None] | None = None,
        project_notes: str = "",
    ) -> None:
        self._run_id = run_id
        self._agents = agents
        self._repo = repo
        self._tests = tests
        self._finalize = finalize
        self._store = store
        self._budget = budget
        self._config = config or MachineConfig()
        self._on_progress = on_progress or (lambda message: None)
        self._project_notes = project_notes
        self._loops = LoopDetector()
        self._memory = _Memory()
        self._counters = Counters()
        self._task = ""

    def run(self, task: str) -> OrchestratorResult:
        self._task = task
        handlers: dict[State, Callable[[], Event]] = {
            State.PLAN: self._plan,
            State.CODE: self._code,
            State.TEST: self._test,
            State.REVIEW: self._review,
            State.OPEN_PR: self._open_pr,
        }
        state = State.PLAN
        while state not in TERMINAL_STATES:
            if reason := self._budget.exceeded():
                event = self._abort(reason)
            else:
                self._on_progress(f"{state.upper()} (iteration {self._counters.iterations})")
                event = handlers[state]()
            state, self._counters = transition(state, event, self._counters, self._config)

        summary = self._summary()
        if state is State.DONE:
            return OrchestratorResult(State.DONE, summary)
        # With no recorded reason, the state machine itself ran out of iterations.
        reason = self._memory.failure_reason or (
            f"reached the limit of {self._config.max_iterations} iterations"
        )
        return OrchestratorResult(State.FAILED, summary, reason, self._failure_report(reason))

    def _summary(self) -> RunSummary:
        memory = self._memory
        return RunSummary(
            task=self._task,
            plan=memory.plan,
            coder_summary=memory.coder_summary,
            review=memory.review,
            tests=memory.tests,
            iterations=self._counters.iterations,
            replans=self._counters.replans,
        )

    def _context(self, sections: list[Section]) -> AgentContext:
        if self._project_notes:
            sections = [Section("project_information", self._project_notes), *sections]
        return AgentContext(self._run_id, self._counters.iterations, sections)

    def _abort(self, reason: str) -> Event:
        self._memory.failure_reason = reason
        return Event.ABORT

    def _agent_failed[T: BaseModel](self, agent: str, result: AgentResult[T]) -> Event:
        return self._abort(f"{agent} stopped: {result.status} ({result.error})")

    def _loop(self, reason: str) -> Event:
        self._memory.history.append(f"Loop detected: {reason}")
        self._memory.failure_reason = f"loop detected: {reason}"
        return Event.LOOP_DETECTED

    def _plan(self) -> Event:
        sections: list[Section] = []
        if self._counters.replans > 0:
            sections.append(Section("previous_failed_attempt", self._attempt_summary()))
            # Start from clean code, or the new plan inherits the approach that just failed.
            self._repo.reset_to_base()
            self._loops.reset()
            self._memory.failing_output = ""
            self._memory.feedback = []
            self._memory.no_changes_note = ""
            self._memory.coder_test_edits = set()
            self._memory.failure_reason = None

        result = self._agents.planner.run(self._task, self._context(sections))
        if result.status != "finished" or result.output is None:
            return self._agent_failed("planner", result)
        self._memory.plan = result.output
        self._memory.history.append("Planned the change.")
        return Event.PLAN_READY

    def _plan_text(self) -> str:
        return format_plan(self._memory.plan) if self._memory.plan else "(no plan)"

    def _code(self) -> Event:
        memory = self._memory
        sections = [Section("plan", self._plan_text())]
        current = self._repo.diff()
        if current:
            sections.append(Section("current_diff", _clip_diff(current)))
        if memory.failing_output:
            sections.append(Section("failing_tests", memory.failing_output))
        if memory.feedback:
            sections.append(Section("reviewer_feedback", "\n".join(memory.feedback)))
        if memory.no_changes_note:
            sections.append(Section("note", memory.no_changes_note))

        before = self._repo.snapshot()
        result = self._agents.coder.run(self._task, self._context(sections))
        if result.status != "finished" or result.output is None:
            return self._agent_failed("coder", result)
        memory.coder_summary = result.output.summary
        memory.history.append(f"Iteration {self._counters.iterations}: {memory.coder_summary}")

        changed = self._repo.changed_paths(before, self._repo.snapshot())
        memory.coder_test_edits.update(path for path in changed if self._repo.is_test_path(path))

        # Tests are left out of the comparison, because the Tester changes them between
        # passes, which would make an unchanged fix look like new work. A pass that only
        # edited tests is not compared at all: the fix staying the same is expected there.
        diff = self._repo.diff()
        edited_only_tests = bool(changed) and all(self._repo.is_test_path(p) for p in changed)
        source_diff = without_paths(diff, self._repo.is_test_path)
        if not edited_only_tests and self._loops.record_diff(source_diff):
            return self._loop("the Coder produced the same change twice")
        if not diff:
            memory.no_changes_note = "Your last attempt changed nothing. The task needs a change."
            return Event.NO_CHANGES
        memory.no_changes_note = ""
        return Event.CODE_DONE

    def _test(self) -> Event:
        memory = self._memory
        sections = [
            Section("plan", self._plan_text()),
            Section("diff", _clip_diff(self._repo.diff())),
        ]
        before = self._repo.snapshot()
        result = self._agents.tester.run(self._task, self._context(sections))
        if result.status != "finished" or result.output is None:
            return self._agent_failed("tester", result)

        changed = self._repo.changed_paths(before, self._repo.snapshot())
        forbidden = [path for path in changed if not self._repo.is_test_path(path)]
        if forbidden:
            self._repo.restore(before, forbidden)
            memory.history.append(f"Undid Tester changes to non-test files: {forbidden}")

        report = self._verify_tests()
        memory.tests = report
        if report.passed:
            memory.failing_output = ""
            return Event.TESTS_PASSED

        memory.failing_output = clip(report.output, _TEST_HEAD_CHARS, _TEST_TAIL_CHARS)
        if result.output.problems:
            memory.failing_output += "\n\nTester's notes:\n" + "\n".join(result.output.problems)
        memory.history.append(f"Tests failed: {', '.join(report.fingerprints)}")
        if self._loops.record_failures(report.fingerprints):
            return self._loop("the same test failed with the same error three times")
        return Event.TESTS_FAILED

    def _verify_tests(self) -> TestReport:
        step_id = self._store.start_step(
            self._run_id, "verify", iteration=self._counters.iterations
        )
        report = self._tests.run()
        self._store.finish_step(
            step_id,
            "passed" if report.passed else "failed",
            summary=clip(report.output, _TEST_HEAD_CHARS, _TEST_TAIL_CHARS),
        )
        return report

    def _review(self) -> Event:
        memory = self._memory
        output = memory.tests.output if memory.tests else ""
        sections = [
            Section("plan", self._plan_text()),
            Section("diff", _clip_diff(self._repo.diff())),
            Section("verified_test_results", clip(output, _TEST_HEAD_CHARS, _TEST_TAIL_CHARS)),
        ]
        if memory.coder_test_edits:
            sections.append(
                Section(
                    "test_files_edited_by_the_coder",
                    "Check that each edit is justified:\n"
                    + "\n".join(sorted(memory.coder_test_edits)),
                )
            )
        result = self._agents.reviewer.run(self._task, self._context(sections))
        if result.status != "finished" or result.output is None:
            return self._agent_failed("reviewer", result)

        review = result.output
        memory.review = review
        if review.verdict == "approve":
            memory.feedback = []
            memory.history.append("Reviewer approved.")
            return Event.APPROVED
        memory.feedback = review.feedback or [review.summary]
        memory.history.append(f"Reviewer requested changes: {review.summary}")
        return Event.CHANGES_REQUESTED

    def _open_pr(self) -> Event:
        try:
            self._finalize(self._summary())
        except FinalizeError as exc:
            return self._abort(f"could not finalize: {exc}")
        return Event.PR_OPENED

    def _attempt_summary(self) -> str:
        memory = self._memory
        parts = ["\n".join(f"- {entry}" for entry in memory.history)]
        if memory.failing_output:
            parts.append(f"Last failing tests:\n{memory.failing_output}")
        if memory.feedback:
            parts.append("Last reviewer feedback:\n" + "\n".join(memory.feedback))
        return "\n\n".join(parts)

    def _failure_report(self, reason: str) -> str:
        memory = self._memory
        lines = [
            "# DevSwarm failure report",
            "",
            f"**Task:** {self._task}",
            f"**Reason:** {reason}",
            f"**Iterations:** {self._counters.iterations}",
            f"**Re-plans:** {self._counters.replans}",
            f"**Cost:** ${self._budget.spent_usd:.6f}",
            "",
            "## What was tried",
            *[f"- {entry}" for entry in memory.history],
        ]
        if memory.plan:
            lines += ["", "## Last plan", "", self._plan_text()]
        if memory.failing_output:
            tail = memory.failing_output[-_SUMMARY_TAIL_CHARS:]
            lines += ["", "## Last failing tests", "", "```text", tail, "```"]
        if memory.feedback:
            lines += ["", "## Last reviewer feedback", *[f"- {item}" for item in memory.feedback]]
        return "\n".join(lines) + "\n"


def _clip_diff(diff: str) -> str:
    return clip(diff, _DIFF_HEAD_CHARS, _DIFF_TAIL_CHARS)
