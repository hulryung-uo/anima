"""Tests for EventBus pub/sub system."""

from anima.core.bus import EventBus


def test_publish_subscribe():
    bus = EventBus()
    received = []
    bus.subscribe("test.event", lambda t, d: received.append((t, d)))
    bus.publish("test.event", {"value": 42})
    assert len(received) == 1
    assert received[0] == ("test.event", {"value": 42})


def test_wildcard_subscribe():
    bus = EventBus()
    received = []
    bus.subscribe("avatar.*", lambda t, d: received.append(t))
    bus.publish("avatar.position", {"x": 1, "y": 2})
    bus.publish("avatar.health", {"hp": 50})
    bus.publish("action.start", {"action": "chop"})  # should not match
    assert received == ["avatar.position", "avatar.health"]


def test_star_subscribe_all():
    bus = EventBus()
    received = []
    bus.subscribe("*", lambda t, d: received.append(t))
    bus.publish("avatar.position", {})
    bus.publish("action.start", {})
    bus.publish("brain.think", {})
    assert len(received) == 3


def test_unsubscribe():
    bus = EventBus()
    received = []
    sub = bus.subscribe("test.*", lambda t, d: received.append(t))
    bus.publish("test.a", {})
    bus.unsubscribe(sub)
    bus.publish("test.b", {})
    assert received == ["test.a"]


def test_multiple_subscribers():
    bus = EventBus()
    r1, r2 = [], []
    bus.subscribe("x", lambda t, d: r1.append(d))
    bus.subscribe("x", lambda t, d: r2.append(d))
    bus.publish("x", {"v": 1})
    assert len(r1) == 1
    assert len(r2) == 1


def test_subscriber_exception_doesnt_crash():
    bus = EventBus()
    received = []

    def bad_callback(t, d):
        raise RuntimeError("oops")

    bus.subscribe("test", bad_callback)
    bus.subscribe("test", lambda t, d: received.append(d))
    bus.publish("test", {"ok": True})
    # Second subscriber should still receive
    assert len(received) == 1


def test_recent_history():
    bus = EventBus()
    for i in range(10):
        bus.publish("tick", {"i": i})
    events = bus.recent(5)
    assert len(events) == 5
    assert events[-1].data["i"] == 9


def test_recent_with_filter():
    bus = EventBus()
    bus.publish("avatar.pos", {"x": 1})
    bus.publish("action.start", {"a": "chop"})
    bus.publish("avatar.hp", {"hp": 50})
    events = bus.recent(10, topic_filter="avatar.*")
    assert len(events) == 2


def test_subscriber_count():
    bus = EventBus()
    assert bus.subscriber_count == 0
    s1 = bus.subscribe("a", lambda t, d: None)
    bus.subscribe("b", lambda t, d: None)
    assert bus.subscriber_count == 2
    bus.unsubscribe(s1)
    assert bus.subscriber_count == 1
