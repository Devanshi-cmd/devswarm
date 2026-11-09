import pytest
from fakes import FakeSandbox, result

from devswarm.orchestrator.loops import LoopDetector, without_paths
from devswarm.orchestrator.state_machine import (
    Counters,
    Event,
    InvalidTransition,
    MachineConfig,
    State,
    transition,
)
from devswarm.orchestrator.verify import SandboxTestRunner
from devswarm.sandbox.images import PythonProfile

CONFIG = MachineConfig()
START = Counters()


@pytest.mark.parametrize(
    ("state", "event", "expected"),
    [
        (State.PLAN, Event.PLAN_READY, State.CODE),
        (State.CODE, Event.CODE_DONE, State.TEST),
        (State.TEST, Event.TESTS_PASSED, State.REVIEW),
        (State.REVIEW, Event.APPROVED, State.OPEN_PR),
        (State.OPEN_PR, Event.PR_OPENED, State.DONE),
    ],
)
def test_happy_path(state: State, event: Event, expected: State) -> None:
    assert transition(state, event, START, CONFIG) == (expected, START)


@pytest.mark.parametrize(
    ("state", "event"),
    [
        (State.TEST, Event.TESTS_FAILED),
        (State.REVIEW, Event.CHANGES_REQUESTED),
        (State.CODE, Event.NO_CHANGES),
    ],
)
def test_going_back_to_code_uses_an_iteration(state: State, event: Event) -> None:
    assert transition(state, event, START, CONFIG) == (State.CODE, Counters(iterations=2))


@pytest.mark.parametrize(
    ("state", "event"),
    [(State.TEST, Event.TESTS_FAILED), (State.REVIEW, Event.CHANGES_REQUESTED)],
)
def test_iteration_limit_fails_the_run(state: State, event: Event) -> None:
    last = Counters(iterations=5)
    assert transition(state, event, last, CONFIG) == (State.FAILED, last)


def test_exactly_five_code_passes_are_allowed() -> None:
    counters = START
    passes = 1
    while True:
        state, counters = transition(State.TEST, Event.TESTS_FAILED, counters, CONFIG)
        if state is State.FAILED:
            break
        passes += 1
    assert passes == 5


def test_reviewer_off_skips_review() -> None:
    config = MachineConfig(reviewer_enabled=False)
    assert transition(State.TEST, Event.TESTS_PASSED, START, config)[0] is State.OPEN_PR


@pytest.mark.parametrize("state", [State.CODE, State.TEST, State.REVIEW])
def test_first_loop_replans_without_refunding_iterations(state: State) -> None:
    counters = Counters(iterations=3)
    assert transition(state, Event.LOOP_DETECTED, counters, CONFIG) == (
        State.PLAN,
        Counters(iterations=3, replans=1),
    )


def test_second_loop_gives_up() -> None:
    counters = Counters(replans=1)
    assert transition(State.CODE, Event.LOOP_DETECTED, counters, CONFIG)[0] is State.FAILED


@pytest.mark.parametrize("state", [State.PLAN, State.CODE, State.TEST, State.REVIEW, State.OPEN_PR])
def test_abort_works_from_any_active_state(state: State) -> None:
    assert transition(state, Event.ABORT, START, CONFIG)[0] is State.FAILED


@pytest.mark.parametrize(
    ("state", "event"),
    [
        (State.PLAN, Event.APPROVED),
        (State.CODE, Event.TESTS_PASSED),
        (State.TEST, Event.PR_OPENED),
        (State.REVIEW, Event.PLAN_READY),
        (State.OPEN_PR, Event.TESTS_FAILED),
    ],
)
def test_nonsense_events_raise(state: State, event: Event) -> None:
    with pytest.raises(InvalidTransition):
        transition(state, event, START, CONFIG)


@pytest.mark.parametrize("state", [State.DONE, State.FAILED])
def test_terminal_states_cannot_be_left(state: State) -> None:
    with pytest.raises(InvalidTransition):
        transition(state, Event.ABORT, START, CONFIG)


def test_same_diff_is_detected_on_second_sight() -> None:
    detector = LoopDetector()
    assert detector.record_diff("+fix") is False
    assert detector.record_diff("+fix") is True


def test_different_diff_is_not_a_loop() -> None:
    detector = LoopDetector()
    detector.record_diff("+one")
    assert detector.record_diff("+two") is False


def test_alternating_between_two_diffs_is_detected() -> None:
    detector = LoopDetector()
    detector.record_diff("+a")
    detector.record_diff("+b")
    assert detector.record_diff("+a") is True


def test_blob_hashes_do_not_affect_comparison() -> None:
    detector = LoopDetector()
    detector.record_diff("diff --git a/x b/x\nindex 111aaa..222bbb 100644\n+fix\n")
    assert detector.record_diff("diff --git a/x b/x\nindex 333ccc..444ddd 100644\n+fix\n")


def test_failure_needs_three_sightings() -> None:
    detector = LoopDetector()
    assert detector.record_failures(["t|ValueError"]) is False
    assert detector.record_failures(["t|ValueError"]) is False
    assert detector.record_failures(["t|ValueError"]) is True


def test_different_failures_do_not_add_up() -> None:
    detector = LoopDetector()
    for fingerprint in ("a|E", "b|E", "c|E", "d|E"):
        assert detector.record_failures([fingerprint]) is False


def test_duplicates_within_one_run_count_once() -> None:
    detector = LoopDetector()
    assert detector.record_failures(["a|E", "a|E", "a|E"]) is False


def test_reset_forgets_everything() -> None:
    detector = LoopDetector()
    detector.record_diff("+x")
    detector.record_failures(["a|E", "a|E"])
    detector.reset()
    assert detector.record_diff("+x") is False
    assert detector.record_failures(["a|E"]) is False


DIFF = (
    "diff --git a/calc.py b/calc.py\n+fix\n"
    "diff --git a/tests/test_a.py b/tests/test_a.py\n+test\n"
    "diff --git a/other.py b/other.py\n+other\n"
)


def test_without_paths_removes_only_matching_sections() -> None:
    kept = without_paths(DIFF, lambda path: path.startswith("tests/"))
    assert "+fix" in kept and "+other" in kept
    assert "+test" not in kept


def test_without_paths_can_remove_everything() -> None:
    assert without_paths(DIFF, lambda path: True) == ""


@pytest.mark.parametrize(
    "path",
    [
        "tests/a.py",
        "pkg/tests/helper.py",
        "test_a.py",
        "pkg/a_test.py",
        "conftest.py",
        "test/x.txt",
    ],
)
def test_test_paths(path: str) -> None:
    assert PythonProfile().is_test_path(path)


@pytest.mark.parametrize("path", ["calc.py", "contest.py", "latest.py", "pkg/testing.py"])
def test_source_paths(path: str) -> None:
    assert not PythonProfile().is_test_path(path)


def test_fingerprints_ignore_changing_message_details() -> None:
    first = "FAILED a.py::t - ValueError: bad value at 0x7f001\n"
    second = "FAILED a.py::t - ValueError: bad value at 0x8e992\n"
    profile = PythonProfile()
    assert profile.failure_fingerprints(first) == profile.failure_fingerprints(second)


def test_fingerprints_of_several_failures() -> None:
    output = (
        "=== short test summary info ===\n"
        "FAILED tests/a.py::test_one - assert 4 == 3\n"
        "FAILED tests/a.py::test_two[1 + 2] - ZeroDivisionError: division by zero\n"
        "ERROR tests/b.py - ModuleNotFoundError: No module named 'x'\n"
        "1 failed in 0.1s\n"
    )
    assert PythonProfile().failure_fingerprints(output) == [
        "tests/a.py::test_one|assertion",
        "tests/a.py::test_two[1 + 2]|ZeroDivisionError",
        "tests/b.py|ModuleNotFoundError",
    ]


def test_runner_passes_on_exit_zero() -> None:
    report = SandboxTestRunner(FakeSandbox(result(stdout="1 passed")), PythonProfile()).run()
    assert report.passed and report.fingerprints == ()


def test_runner_fails_on_nonzero_even_if_output_looks_good() -> None:
    report = SandboxTestRunner(
        FakeSandbox(result(exit_code=1, stdout="all passed")), PythonProfile()
    ).run()
    assert not report.passed
    assert report.fingerprints == ("exit:1",)


def test_no_tests_collected_is_a_failure() -> None:
    report = SandboxTestRunner(FakeSandbox(result(exit_code=5)), PythonProfile()).run()
    assert not report.passed


def test_timeout_has_a_fingerprint() -> None:
    report = SandboxTestRunner(
        FakeSandbox(result(exit_code=-1, timed_out=True)), PythonProfile()
    ).run()
    assert report.fingerprints == ("timeout",)
