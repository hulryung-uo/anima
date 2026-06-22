"""Planner._failed_destinations must stay bounded across a long roam.

Regression: every distinct failed move destination (x, y) was inserted into
``_failed_destinations`` and never removed. The read sites only treat an entry
as "blocked" while ``now - v < ttl`` (300 s), so older entries are dead weight,
yet they accumulated forever — across a long roam the dict grew without bound.

``_record_failed_destination`` now prunes entries past the TTL on every insert.
This is a pure memory bound: live (within-TTL) entries — the only ones callers
act on — are preserved, so the cooldown semantics are unchanged.
"""

from __future__ import annotations

import time

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import ProcedureRegistry


@pytest.fixture
def planner() -> Planner:
    return Planner(ProcedureRegistry())


def test_failed_destinations_pruned_after_ttl(planner: Planner) -> None:
    ttl = planner._failed_dest_ttl_s
    now = time.time()

    # Simulate a long roam: 1000 distinct destinations failed long ago
    # (past the TTL). Without pruning they would all linger forever.
    for i in range(1000):
        planner._record_failed_destination(i, 0)
    # Backdate them to well beyond the TTL.
    planner._failed_destinations = {
        k: now - (ttl + 60.0) for k in planner._failed_destinations
    }
    assert len(planner._failed_destinations) == 1000

    # One more failure (the path that grows the dict) must prune the dead
    # entries — leaving only the fresh insert.
    planner._record_failed_destination(9999, 9999)
    assert len(planner._failed_destinations) == 1
    assert (9999, 9999) in planner._failed_destinations


def test_live_entries_are_not_pruned(planner: Planner) -> None:
    """Cooldown semantics preserved: within-TTL entries survive a new insert."""
    now = time.time()
    ttl = planner._failed_dest_ttl_s

    planner._record_failed_destination(1, 1)
    planner._record_failed_destination(2, 2)
    # Age them to just under the TTL — still "blocked", must be kept.
    planner._failed_destinations = {
        k: now - (ttl - 1.0) for k in planner._failed_destinations
    }

    planner._record_failed_destination(3, 3)

    assert (1, 1) in planner._failed_destinations
    assert (2, 2) in planner._failed_destinations
    assert (3, 3) in planner._failed_destinations
    assert len(planner._failed_destinations) == 3


def test_growth_stays_bounded_across_long_roam(
    planner: Planner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Many failures spread over a long simulated roam must not grow unbounded.

    Simulated time advances past the TTL between each failure, so the prior
    insert is always expired by the next one — the dict cannot accumulate one
    key per distinct destination forever.
    """
    import anima.planner.planner as planner_mod

    fake_now = {"t": 1_000_000.0}
    monkeypatch.setattr(planner_mod._time, "time", lambda: fake_now["t"])

    sizes: list[int] = []
    for i in range(2000):
        planner._record_failed_destination(i, i)
        sizes.append(len(planner._failed_destinations))
        # Advance past the TTL so the entry just inserted is expired by the
        # time the next failure is recorded.
        fake_now["t"] += planner._failed_dest_ttl_s + 1.0

    # The dict never grows past a tiny constant — old keys are pruned on insert.
    assert max(sizes) <= 1
