import pytest

import scheduler


def test_chain():
    assert scheduler.schedule({"a": [], "b": ["a"], "c": ["b"]}) == ["a", "b", "c"]


def test_alphabetical_among_ready():
    assert scheduler.schedule({"c": [], "a": [], "b": []}) == ["a", "b", "c"]


def test_newly_ready_task_competes_alphabetically():
    tasks = {"z": [], "b": ["z"], "m": [], "a": ["m"]}
    assert scheduler.schedule(tasks) == ["m", "a", "z", "b"]


def test_cycle():
    with pytest.raises(scheduler.CycleError) as caught:
        scheduler.schedule({"x": [], "a": ["b"], "b": ["a"]})
    assert isinstance(caught.value, ValueError)
    assert "a" in str(caught.value) and "b" in str(caught.value)


def test_self_dependency_is_a_cycle():
    with pytest.raises(scheduler.CycleError):
        scheduler.schedule({"a": ["a"]})


def test_unknown_dependency():
    with pytest.raises(scheduler.UnknownTaskError) as caught:
        scheduler.schedule({"a": ["ghost"]})
    assert isinstance(caught.value, ValueError)
    assert "ghost" in str(caught.value)


def test_empty():
    assert scheduler.schedule({}) == []
