import pytest

import events


def bus_with_log():
    return events.EventBus(), []


def test_self_unsubscribe_does_not_skip_next():
    bus, log = bus_with_log()

    def first(_):
        log.append("first")
        stop()

    stop = bus.subscribe("e", first)
    bus.subscribe("e", lambda _: log.append("second"))
    bus.publish("e")
    assert log == ["first", "second"]
    bus.publish("e")
    assert log == ["first", "second", "second"]


def test_subscribing_during_publish_waits_for_next_publish():
    bus, log = bus_with_log()

    def first(_):
        log.append("first")
        if len(log) == 1:
            bus.subscribe("e", lambda _: log.append("late"))

    bus.subscribe("e", first)
    bus.publish("e")
    assert log == ["first"]
    bus.publish("e")
    assert log == ["first", "first", "late"]


def test_unsubscribing_a_later_handler_prevents_its_call():
    bus, log = bus_with_log()

    def first(_):
        log.append("first")
        stop_second()

    bus.subscribe("e", first)
    stop_second = bus.subscribe("e", lambda _: log.append("second"))
    bus.subscribe("e", lambda _: log.append("third"))
    bus.publish("e")
    assert log == ["first", "third"]


def test_priority_order_and_ties():
    bus, log = bus_with_log()
    bus.subscribe("e", lambda _: log.append("low"), priority=1)
    bus.subscribe("e", lambda _: log.append("high-1"), priority=5)
    bus.subscribe("e", lambda _: log.append("high-2"), priority=5)
    bus.subscribe("e", lambda _: log.append("default"))
    bus.publish("e")
    assert log == ["high-1", "high-2", "low", "default"]


def test_all_handlers_run_and_errors_are_collected():
    bus, log = bus_with_log()
    first_error, second_error = ValueError("a"), KeyError("b")

    def fail(error):
        def handler(_):
            raise error

        return handler

    bus.subscribe("e", fail(first_error))
    bus.subscribe("e", lambda _: log.append("ran"))
    bus.subscribe("e", fail(second_error))
    with pytest.raises(events.PublishError) as caught:
        bus.publish("e")
    assert log == ["ran"]
    assert caught.value.errors == [first_error, second_error]


def test_no_error_when_nothing_fails():
    bus, log = bus_with_log()
    bus.subscribe("e", log.append)
    bus.publish("e", 1)
    assert log == [1]


def test_double_unsubscribe_is_harmless():
    bus, log = bus_with_log()
    stop = bus.subscribe("e", log.append)
    stop()
    stop()
    bus.publish("e", 1)
    assert log == []


def test_same_handler_twice_is_tracked_separately():
    bus, log = bus_with_log()
    stop_first = bus.subscribe("e", log.append)
    bus.subscribe("e", log.append)
    bus.publish("e", 1)
    assert log == [1, 1]
    stop_first()
    stop_first()
    bus.publish("e", 2)
    assert log == [1, 1, 2]


def test_unknown_event_is_fine():
    events.EventBus().publish("nothing")
