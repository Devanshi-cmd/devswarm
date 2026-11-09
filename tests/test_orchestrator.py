"""The orchestrator with fake agents: no LLM, no Docker, no git."""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from devswarm.agents.base import AgentContext, AgentResult, AgentStatus
from devswarm.agents.coder import CoderOutput
from devswarm.agents.planner import PlanOutput
from devswarm.agents.reviewer import ReviewOutput
from devswarm.agents.tester import TesterOutput
from devswarm.llm.pricing import TokenUsage
from devswarm.orchestrator.budget import Budget, BudgetLimits
from devswarm.orchestrator.engine import (
    Agents,
    FinalizeError,
    Orchestrator,
    OrchestratorResult,
    RunSummary,
)
from devswarm.orchestrator.state_machine import MachineConfig, State
from devswarm.orchestrator.verify import TestReport
from devswarm.storage.redact import Redactor
from devswarm.storage.trace import TraceStore

PLAN = PlanOutput(steps=["s"], files_to_touch=["a.py"], acceptance_criteria=["ok"], risks=[])
APPROVE = ReviewOutput(verdict="approve", summary="good", feedback=[])
PASS = TestReport(True, 0, "1 passed")


def fail(fingerprint: str = "t|ValueError") -> TestReport:
    return TestReport(False, 1, f"FAILED output for {fingerprint}", (fingerprint,))


def changes(*feedback: str) -> ReviewOutput:
    return ReviewOutput(verdict="request_changes", summary="fix it", feedback=list(feedback))


class FakeRepo:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.snapshots: dict[str, dict[str, str]] = {}
        self.resets = 0

    def snapshot(self) -> str:
        name = f"tree{len(self.snapshots)}"
        self.snapshots[name] = dict(self.files)
        return name

    def changed_paths(self, before: str, after: str) -> list[str]:
        a, b = self.snapshots[before], self.snapshots[after]
        return sorted(path for path in {*a, *b} if a.get(path) != b.get(path))

    def restore(self, snapshot: str, paths: list[str]) -> None:
        saved = self.snapshots[snapshot]
        for path in paths:
            if path in saved:
                self.files[path] = saved[path]
            else:
                del self.files[path]

    def diff(self) -> str:
        return "".join(
            f"diff --git a/{path} b/{path}\n+{self.files[path]}\n" for path in sorted(self.files)
        )

    def reset_to_base(self) -> None:
        self.resets += 1
        self.files.clear()

    def is_test_path(self, path: str) -> bool:
        return path.startswith("test_")


class FakeAgent:
    """Replays outcomes. Each item is an output, or a (status, error) failure."""

    def __init__(self, *outcomes: Any, default: Any = None, effect: Any = None) -> None:
        self.outcomes = list(outcomes)
        self.default = default
        self.effect: Callable[[int], None] | None = effect
        self.contexts: list[AgentContext] = []

    def titles(self, call: int = -1) -> list[str]:
        return [section.title for section in self.contexts[call].sections]

    def section(self, title: str, call: int = -1) -> str:
        return next(s.text for s in self.contexts[call].sections if s.title == title)

    def run(self, task: str, context: AgentContext) -> AgentResult[Any]:
        self.contexts.append(context)
        if self.effect:
            self.effect(len(self.contexts))
        outcome = self.outcomes.pop(0) if self.outcomes else self.default
        if isinstance(outcome, tuple):
            status: AgentStatus = outcome[0]
            return AgentResult(status, 0, 1, error=outcome[1])
        return AgentResult("finished", 0, 1, output=outcome)


class FakeTests:
    def __init__(self, *reports: TestReport, default: TestReport = PASS) -> None:
        self.reports = list(reports)
        self.default = default
        self.runs = 0

    def run(self) -> TestReport:
        self.runs += 1
        return self.reports.pop(0) if self.reports else self.default


class Harness:
    def __init__(
        self,
        store: TraceStore,
        tests: FakeTests | None = None,
        reviews: list[Any] | None = None,
        config: MachineConfig | None = None,
        limits: BudgetLimits | None = None,
        same_fix: bool = False,
    ) -> None:
        self.repo = FakeRepo()

        def coder_effect(call: int) -> None:
            self.repo.files["a.py"] = "fix" if same_fix else f"fix{call}"

        self.planner = FakeAgent(default=PLAN)
        self.coder = FakeAgent(
            default=CoderOutput(summary="changed a.py", files_changed=["a.py"]),
            effect=coder_effect,
        )
        self.tester = FakeAgent(default=TesterOutput(summary="t", test_files=[], problems=[]))
        self.reviewer = FakeAgent(*(reviews or []), default=APPROVE)
        self.tests = tests or FakeTests()
        self.finalized: list[RunSummary] = []
        self.budget = Budget(limits)
        self.store = store
        self.run_id = store.start_run("t")
        self.progress: list[str] = []
        self.finalize_error: str | None = None
        self.config = config

    def _finalize(self, summary: RunSummary) -> None:
        if self.finalize_error:
            raise FinalizeError(self.finalize_error)
        self.finalized.append(summary)

    def run(self, task: str = "fix the bug") -> OrchestratorResult:
        return Orchestrator(
            run_id=self.run_id,
            agents=Agents(self.planner, self.coder, self.tester, self.reviewer),
            repo=self.repo,
            tests=self.tests,
            finalize=self._finalize,
            store=self.store,
            budget=self.budget,
            config=self.config,
            on_progress=self.progress.append,
        ).run(task)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TraceStore]:
    with TraceStore(tmp_path / "t.sqlite3", Redactor()) as trace:
        yield trace


def test_happy_path(store: TraceStore) -> None:
    harness = Harness(store)
    result = harness.run()
    assert result.status is State.DONE
    assert len(harness.finalized) == 1
    assert harness.finalized[0].plan == PLAN
    assert harness.finalized[0].review == APPROVE
    assert harness.finalized[0].tests == PASS
    assert result.failure_report is None


def test_failing_tests_return_to_coder_with_output(store: TraceStore) -> None:
    harness = Harness(store, tests=FakeTests(fail()))
    assert harness.run().status is State.DONE
    assert len(harness.coder.contexts) == 2
    assert "FAILED output" in harness.coder.section("failing_tests")
    assert "failing_tests" not in harness.coder.titles(0)


def test_review_feedback_reaches_the_coder(store: TraceStore) -> None:
    harness = Harness(store, reviews=[changes("handle None in foo()")])
    assert harness.run().status is State.DONE
    assert "handle None in foo()" in harness.coder.section("reviewer_feedback")


def test_review_summary_used_when_feedback_is_empty(store: TraceStore) -> None:
    harness = Harness(store, reviews=[changes()])
    harness.run()
    assert "fix it" in harness.coder.section("reviewer_feedback")


def test_reviewer_can_be_turned_off(store: TraceStore) -> None:
    harness = Harness(store, config=MachineConfig(reviewer_enabled=False))
    assert harness.run().status is State.DONE
    assert harness.reviewer.contexts == []


def test_agents_get_summaries_only(store: TraceStore) -> None:
    harness = Harness(store)
    harness.run()
    assert harness.planner.titles() == []
    assert harness.coder.titles() == ["plan"]
    assert harness.tester.titles() == ["plan", "diff"]
    assert harness.reviewer.titles() == ["plan", "diff", "verified_test_results"]


def test_reviewer_is_told_tests_passed_from_the_real_run(store: TraceStore) -> None:
    harness = Harness(store, tests=FakeTests(TestReport(True, 0, "42 passed in 1s")))
    harness.run()
    assert "42 passed" in harness.reviewer.section("verified_test_results")


def test_iteration_limit(store: TraceStore) -> None:
    fails = FakeTests(
        fail("a|E"), fail("b|E"), default=fail("c|E")
    )  # Distinct, so no loop is detected early.
    harness = Harness(store, tests=fails, config=MachineConfig(max_iterations=2))
    result = harness.run()
    assert result.status is State.FAILED
    assert "limit of 2 iterations" in (result.reason or "")
    assert len(harness.coder.contexts) == 2
    assert harness.finalized == []


def test_same_diff_triggers_replan_then_gives_up(store: TraceStore) -> None:
    harness = Harness(store, reviews=[changes("x")], same_fix=True)
    harness.reviewer.default = changes("x")
    result = harness.run()
    assert result.status is State.FAILED
    assert "same change twice" in (result.reason or "")
    assert len(harness.planner.contexts) == 2
    assert harness.repo.resets == 1
    assert result.summary.replans == 1


def test_replan_is_told_what_failed(store: TraceStore) -> None:
    harness = Harness(store, reviews=[changes("x")], same_fix=True)
    harness.reviewer.default = changes("x")
    harness.run()
    attempt = harness.planner.section("previous_failed_attempt")
    assert "changed a.py" in attempt
    assert "Loop detected" in attempt


def test_replan_can_succeed(store: TraceStore) -> None:
    harness = Harness(store, reviews=[changes("x")], same_fix=True)
    # After the reset the same fix is new again, so the run can finish.
    result = harness.run()
    assert result.status is State.DONE
    assert result.summary.replans == 1


def test_three_identical_failures_trigger_replan(store: TraceStore) -> None:
    harness = Harness(store, tests=FakeTests(fail(), fail(), fail()))
    result = harness.run()
    assert result.status is State.DONE
    assert result.summary.replans == 1
    assert len(harness.planner.contexts) == 2


def test_no_changes_gets_one_more_try_with_a_note(store: TraceStore) -> None:
    harness = Harness(store)

    def effect(call: int) -> None:
        if call == 2:
            harness.repo.files["a.py"] = "real fix"

    harness.coder.effect = effect
    assert harness.run().status is State.DONE
    assert "changed nothing" in harness.coder.section("note", 1)
    assert harness.tester.contexts != []


def test_twice_no_changes_is_a_loop(store: TraceStore) -> None:
    harness = Harness(store, config=MachineConfig(max_replans=0))
    harness.coder.effect = lambda call: None
    result = harness.run()
    assert result.status is State.FAILED
    assert harness.tester.contexts == []


def test_coder_editing_only_tests_is_not_a_loop(store: TraceStore) -> None:
    harness = Harness(store, reviews=[changes("test is wrong")])

    def effect(call: int) -> None:
        if call == 1:
            harness.repo.files["a.py"] = "fix"
        else:
            harness.repo.files["test_a.py"] = "corrected"

    harness.coder.effect = effect
    result = harness.run()
    assert result.status is State.DONE
    assert result.summary.replans == 0


def test_tester_adding_tests_does_not_hide_a_repeated_fix(store: TraceStore) -> None:
    harness = Harness(store, tests=FakeTests(fail("a|E")), same_fix=True)

    def tester_effect(call: int) -> None:
        harness.repo.files[f"test_{call}.py"] = "new test"

    harness.tester.effect = tester_effect
    result = harness.run()
    assert result.summary.replans == 1


def test_tester_changes_to_source_are_undone(store: TraceStore) -> None:
    harness = Harness(store)

    def effect(call: int) -> None:
        harness.repo.files["a.py"] = "sabotaged"
        harness.repo.files["new_source.py"] = "x"
        harness.repo.files["test_ok.py"] = "kept"

    harness.tester.effect = effect
    assert harness.run().status is State.DONE
    assert harness.repo.files["a.py"] == "fix1"
    assert "new_source.py" not in harness.repo.files
    assert harness.repo.files["test_ok.py"] == "kept"


def test_coder_test_edits_are_flagged_to_the_reviewer(store: TraceStore) -> None:
    harness = Harness(store)

    def effect(call: int) -> None:
        harness.repo.files["a.py"] = "fix"
        harness.repo.files["test_weakened.py"] = "assert True"

    harness.coder.effect = effect
    harness.run()
    assert "test_weakened.py" in harness.reviewer.section("test_files_edited_by_the_coder")


def test_no_flag_when_coder_leaves_tests_alone(store: TraceStore) -> None:
    harness = Harness(store)
    harness.run()
    assert "test_files_edited_by_the_coder" not in harness.reviewer.titles()


def test_tester_problems_reach_the_coder(store: TraceStore) -> None:
    harness = Harness(store, tests=FakeTests(fail()))
    harness.tester.outcomes = [TesterOutput(summary="s", test_files=[], problems=["crashes on 0"])]
    harness.run()
    assert "crashes on 0" in harness.coder.section("failing_tests")


@pytest.mark.parametrize("agent", ["planner", "coder", "tester", "reviewer"])
@pytest.mark.parametrize("status", ["refused", "max_turns", "error"])
def test_agent_failure_stops_the_run(store: TraceStore, agent: str, status: AgentStatus) -> None:
    harness = Harness(store)
    getattr(harness, agent).outcomes = [(status, "why")]
    result = harness.run()
    assert result.status is State.FAILED
    assert agent in (result.reason or "")
    assert status in (result.reason or "")
    assert harness.finalized == []


def test_budget_is_checked_between_states(store: TraceStore) -> None:
    harness = Harness(store, limits=BudgetLimits(max_usd=0.5))

    def overspend(call: int) -> None:
        harness.budget.charge(TokenUsage(), 1.0)

    harness.planner.effect = overspend
    result = harness.run()
    assert result.status is State.FAILED
    assert "cost budget" in (result.reason or "")
    assert harness.coder.contexts == []


def test_budget_is_not_refunded_by_a_replan(store: TraceStore) -> None:
    harness = Harness(store, reviews=[changes("x")], same_fix=True)
    harness.budget.charge(TokenUsage(), 0.25)
    harness.run()
    assert harness.budget.spent_usd == 0.25


def test_finalize_failure_fails_the_run(store: TraceStore) -> None:
    harness = Harness(store)
    harness.finalize_error = "push rejected"
    result = harness.run()
    assert result.status is State.FAILED
    assert "push rejected" in (result.reason or "")


def test_failure_report_explains_what_was_tried(store: TraceStore) -> None:
    harness = Harness(
        store, tests=FakeTests(default=fail("a|E")), config=MachineConfig(max_replans=0)
    )
    result = harness.run()
    report = result.failure_report or ""
    assert "same test failed" in report
    assert "changed a.py" in report
    assert "FAILED output" in report
    assert "Last plan" in report


def test_verification_is_recorded_as_a_step(store: TraceStore) -> None:
    harness = Harness(store, tests=FakeTests(fail()))
    harness.run()
    verify = [step for step in store.steps(harness.run_id) if step["agent"] == "verify"]
    assert [step["status"] for step in verify] == ["failed", "passed"]
    assert "FAILED output" in verify[0]["summary"]


def test_tests_run_once_per_test_state(store: TraceStore) -> None:
    harness = Harness(store)
    harness.run()
    assert harness.tests.runs == 1


def test_progress_is_reported(store: TraceStore) -> None:
    harness = Harness(store)
    harness.run()
    assert [message.split()[0] for message in harness.progress] == [
        "PLAN",
        "CODE",
        "TEST",
        "REVIEW",
        "OPEN_PR",
    ]
