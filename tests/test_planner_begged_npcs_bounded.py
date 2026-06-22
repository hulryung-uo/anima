"""ctx.blackboard["_begged_npcs"] must stay bounded across a long roam.

Regression: ``_BegNpcForCoin.run`` stamped ``_begged_npcs[serial] = now`` once
per distinct NPC the agent begged from and never removed entries. The sole
reader (``Planner._find_beg_npc``) only treats an entry as live while
``now - last < COOLDOWN_S`` (30 s) and ignores older ones, so across a long
roam through many towns the dict accumulated one dead key per NPC ever begged
and grew without bound.

``_BegNpcForCoin.prune_begged`` now drops lapsed entries on every insert. This
is a pure memory bound: live (within-cooldown) entries — the only ones the
reader acts on — are preserved, so the cooldown semantics are unchanged.
"""

from __future__ import annotations

import pytest

from anima.planner.planner import _BegNpcForCoin


def test_lapsed_entries_pruned_on_insert() -> None:
    cooldown = _BegNpcForCoin.COOLDOWN_S
    now = 1_000_000.0
    begged: dict[int, float] = {}

    # Simulate a long roam: 1000 distinct NPCs begged long ago (past cooldown).
    for serial in range(1000):
        begged[serial] = now - (cooldown + 60.0)
    assert len(begged) == 1000

    # The insert path prunes the dead entries, leaving only the fresh stamp.
    _BegNpcForCoin.prune_begged(begged, now)
    begged[9999] = now

    assert len(begged) == 1
    assert 9999 in begged


def test_live_entries_are_not_pruned() -> None:
    """Cooldown semantics preserved: within-cooldown entries survive a prune."""
    cooldown = _BegNpcForCoin.COOLDOWN_S
    now = 1_000_000.0
    begged: dict[int, float] = {
        1: now - (cooldown - 1.0),  # still on cooldown — keep
        2: now - (cooldown - 5.0),  # still on cooldown — keep
        3: now - (cooldown + 1.0),  # lapsed — drop
    }

    _BegNpcForCoin.prune_begged(begged, now)

    assert 1 in begged
    assert 2 in begged
    assert 3 not in begged
    assert len(begged) == 2


def test_growth_stays_bounded_across_long_roam() -> None:
    """Many begs spread over a long simulated roam must not grow unbounded.

    Simulated time advances past the cooldown between each beg, so the prior
    insert is always expired by the next one — the dict cannot accumulate one
    key per distinct NPC forever.
    """
    cooldown = _BegNpcForCoin.COOLDOWN_S
    begged: dict[int, float] = {}
    now = 1_000_000.0

    sizes: list[int] = []
    for serial in range(2000):
        # Mirror the run() insert path: prune lapsed, then stamp.
        _BegNpcForCoin.prune_begged(begged, now)
        begged[serial] = now
        sizes.append(len(begged))
        # Advance past the cooldown so the just-inserted entry is expired by
        # the time the next beg is recorded.
        now += cooldown + 1.0

    # The dict never grows past a tiny constant — old keys are pruned on insert.
    assert max(sizes) <= 1


def test_burst_within_cooldown_keeps_all_then_collapses() -> None:
    """A burst of begs inside one cooldown window are all retained..."""
    cooldown = _BegNpcForCoin.COOLDOWN_S
    begged: dict[int, float] = {}
    now = 1_000_000.0

    for serial in range(50):
        _BegNpcForCoin.prune_begged(begged, now)
        begged[serial] = now  # same instant — all live
    assert len(begged) == 50

    # ...then a single beg after the window lapses prunes the whole burst.
    now += cooldown + 1.0
    _BegNpcForCoin.prune_begged(begged, now)
    begged[99] = now
    assert len(begged) == 1
    assert 99 in begged
