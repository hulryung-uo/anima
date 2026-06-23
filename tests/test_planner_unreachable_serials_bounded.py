"""_HuntForGold's per-body unreachable-serial set must stay bounded.

Regression: when the agent could not reach a hunt target, its serial was added
to ``_hunt_unreachable_serials_by_body[body]`` — a set keyed by mob body type.
The only read was ``len(...) >= 2`` (to blacklist the whole body type), so once
a body was blacklisted the accumulated serials were dead weight. But fast prey
(cats, rats) flee faster than the agent can chase and respawn endlessly with
FRESH serials, so this set grew one entry per distinct spawn for the entire
session — an unbounded per-session leak.

``_HuntForGold._record_unreachable_body`` now clears a body's serial set the
moment it trips the body blacklist, so the set can never grow past the
threshold per body while preserving the blacklist semantics.
"""

from __future__ import annotations

from anima.planner.planner import _HuntForGold


def test_blacklist_trips_at_threshold_then_clears() -> None:
    bb: dict = {}
    threshold = _HuntForGold.UNREACHABLE_BODY_THRESHOLD
    body = 0x00C9  # a cat

    tripped = [
        _HuntForGold._record_unreachable_body(bb, body, serial=s, now=100.0)
        for s in range(threshold)
    ]

    # The threshold-th distinct serial trips the body blacklist...
    assert tripped[-1] is True
    assert not any(tripped[:-1])
    assert body in bb["_hunt_unreachable_bodies"]
    # ...and the per-body serial set is dropped so it stops accumulating.
    assert body not in bb["_hunt_unreachable_serials_by_body"]


def test_serial_set_stays_bounded_across_endless_fleeing_prey() -> None:
    """A long soak of fresh serials of one body must not grow without bound."""
    bb: dict = {}
    body = 0x00C9
    threshold = _HuntForGold.UNREACHABLE_BODY_THRESHOLD

    sizes: list[int] = []
    for serial in range(5000):
        _HuntForGold._record_unreachable_body(bb, body, serial=serial, now=100.0)
        fails = bb.get("_hunt_unreachable_serials_by_body", {})
        sizes.append(len(fails.get(body, set())))

    # The per-body serial set never exceeds the trip threshold: it is cleared
    # the instant it reaches it, then re-accumulates from scratch.
    assert max(sizes) <= threshold
    # Total distinct body keys is also bounded (one body here).
    assert len(bb.get("_hunt_unreachable_serials_by_body", {})) <= 1


def test_multiple_bodies_each_bounded() -> None:
    bb: dict = {}
    threshold = _HuntForGold.UNREACHABLE_BODY_THRESHOLD
    bodies = [0x00C9, 0x00D0, 0x0011]

    for serial in range(3000):
        body = bodies[serial % len(bodies)]
        _HuntForGold._record_unreachable_body(bb, body, serial=serial, now=100.0)

    fails = bb.get("_hunt_unreachable_serials_by_body", {})
    # Every distinct body is blacklisted...
    for body in bodies:
        assert body in bb["_hunt_unreachable_bodies"]
    # ...and no body's residual serial set ever exceeds the threshold.
    for body, serials in fails.items():
        assert len(serials) <= threshold


def test_one_unreachable_serial_does_not_blacklist() -> None:
    """A single unreachable serial keeps the body huntable (semantics preserved)."""
    bb: dict = {}
    body = 0x00C9

    tripped = _HuntForGold._record_unreachable_body(
        bb, body, serial=1, now=100.0
    )

    assert tripped is False
    assert body not in bb.get("_hunt_unreachable_bodies", {})
    # The lone serial is retained until a second distinct serial arrives.
    assert bb["_hunt_unreachable_serials_by_body"][body] == {1}
