from events import EventBus


def test_publish():
    received = []
    bus = EventBus()
    bus.subscribe("a", received.append)
    bus.publish("a", 1)
    assert received == [1]


def test_unsubscribe():
    received = []
    bus = EventBus()
    stop = bus.subscribe("a", received.append)
    stop()
    bus.publish("a", 1)
    assert received == []
