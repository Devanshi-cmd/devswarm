"""The run's states and transitions, as a pure function with no LLM or I/O."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class State(StrEnum):
    PLAN = "plan"
    CODE = "code"
    TEST = "test"
    REVIEW = "review"
    OPEN_PR = "open_pr"
    DONE = "done"
    FAILED = "failed"


class Event(StrEnum):
    PLAN_READY = "plan_ready"
    CODE_DONE = "code_done"
    NO_CHANGES = "no_changes"
    TESTS_PASSED = "tests_passed"
    TESTS_FAILED = "tests_failed"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    PR_OPENED = "pr_opened"
    LOOP_DETECTED = "loop_detected"
    ABORT = "abort"


TERMINAL_STATES = frozenset({State.DONE, State.FAILED})


class InvalidTransition(Exception):
    pass


@dataclass(frozen=True)
class MachineConfig:
    max_iterations: int = 5  # How many times CODE may run in total.
    max_replans: int = 1
    reviewer_enabled: bool = True


@dataclass(frozen=True)
class Counters:
    iterations: int = 1  # The current pass through CODE, starting at 1.
    replans: int = 0


def _back_to_code(counters: Counters, config: MachineConfig) -> tuple[State, Counters]:
    if counters.iterations >= config.max_iterations:
        return State.FAILED, counters
    return State.CODE, replace(counters, iterations=counters.iterations + 1)


def transition(
    state: State, event: Event, counters: Counters, config: MachineConfig
) -> tuple[State, Counters]:
    if state in TERMINAL_STATES:
        raise InvalidTransition(f"{state} is a terminal state")

    if event is Event.ABORT:
        return State.FAILED, counters
    if event is Event.LOOP_DETECTED:
        if counters.replans >= config.max_replans:
            return State.FAILED, counters
        return State.PLAN, replace(counters, replans=counters.replans + 1)

    match (state, event):
        case (State.PLAN, Event.PLAN_READY):
            return State.CODE, counters
        case (State.CODE, Event.CODE_DONE):
            return State.TEST, counters
        case (State.CODE, Event.NO_CHANGES):
            return _back_to_code(counters, config)
        case (State.TEST, Event.TESTS_PASSED):
            return (State.REVIEW if config.reviewer_enabled else State.OPEN_PR), counters
        case (State.TEST, Event.TESTS_FAILED) | (State.REVIEW, Event.CHANGES_REQUESTED):
            return _back_to_code(counters, config)
        case (State.REVIEW, Event.APPROVED):
            return State.OPEN_PR, counters
        case (State.OPEN_PR, Event.PR_OPENED):
            return State.DONE, counters
    raise InvalidTransition(f"no transition from {state} on {event}")
