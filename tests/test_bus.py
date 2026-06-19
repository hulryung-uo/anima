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


def test_callback_can_unsubscribe_itself_during_publish():
    """A one-shot listener that unsubscribes itself inside its own callback
    must not crash publish() (no 'dictionary changed size during iteration')."""
    bus = EventBus()
    hits: list[str] = []
    # A second subscriber so the dispatch loop keeps iterating after the
    # mutating callback runs — that's what triggers the RuntimeError on a
    # live .values() iteration.
    bus.subscribe("*", lambda t, d: hits.append("other"))

    sub_box: dict = {}

    def _one_shot(_topic: str, _data: dict) -> None:
        hits.append("one_shot")
        bus.unsubscribe(sub_box["sub"])

    sub_box["sub"] = bus.subscribe("test.event", _one_shot)

    # First publish fires the one-shot and removes it mid-dispatch.
    bus.publish("test.event", {})
    # Second publish must see it gone (fired exactly once) and still deliver
    # to the other subscriber.
    bus.publish("test.event", {})

    assert hits.count("one_shot") == 1
    assert hits.count("other") == 2


def test_callback_can_subscribe_during_publish():
    """A callback that adds a new subscriber mid-dispatch must not crash
    publish(); the newcomer simply isn't invoked for the in-flight event."""
    bus = EventBus()
    late: list[str] = []

    def _late(_topic: str, _data: dict) -> None:
        late.append("late")

    def _adder(_topic: str, _data: dict) -> None:
        bus.subscribe("test.event", _late)

    bus.subscribe("test.event", _adder)

    # Must not raise despite _subs growing during iteration.
    bus.publish("test.event", {})
    # The late subscriber was added after the snapshot, so it sees nothing yet.
    assert late == []
    # But it is wired for subsequent events.
    bus.publish("test.event", {})
    assert late == ["late"]



def test_recent_filter_returns_matches_outside_count2_window():
    """recent(count, topic_filter) must search the whole retained history,
    not a count*2 tail. With many newer non-matching events, the matching
    ones still in history must be returned rather than silently dropped."""
    bus = EventBus()
    # 40 matching events, then 100 non-matching newer ones.
    for i in range(40):
        bus.publish("avatar.hp", {"i": i})
    for _ in range(100):
        bus.publish("noise.x", {})
    # All 40 matching events are still in history (cap is 500); ask for 40.
    got = bus.recent(40, topic_filter="avatar.*")
    assert len(got) == 40
    assert [e.data["i"] for e in got] == list(range(40))


def test_recent_filter_sparse_matches_among_noise():
    """Sparse matches interleaved with a large count of noise must all be
    returned up to `count`, regardless of the count*2 heuristic."""
    bus = EventBus()
    for i in range(50):
        bus.publish("avatar.hp", {"i": i})
        for _ in range(3):
            bus.publish("noise.x", {})
    # 50 matching events spread thin; request the last 50.
    got = bus.recent(50, topic_filter="avatar.*")
    assert len(got) == 50
    assert got[-1].data["i"] == 49
    assert got[0].data["i"] == 0


def test_recent_zero_or_negative_count_returns_empty():
    bus = EventBus()
    bus.publish("avatar.hp", {})
    bus.publish("noise.x", {})
    assert bus.recent(0) == []
    assert bus.recent(-1, topic_filter="avatar.*") == []
