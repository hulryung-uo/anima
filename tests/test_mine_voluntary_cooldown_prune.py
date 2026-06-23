"""Soak-leak guard: ``_find_mineable_tile`` must DROP expired entries from
BOTH cooldown maps (``depleted_banks`` and ``_voluntary_cooldown_banks``)
regardless of where they sit, not only the bank keys it happens to query.

``_is_bank_depleted`` self-prunes a stale entry — but only for the bank keys
within MOVE_RADIUS of the agent's current footing (the only keys ``_check_tile``
ever queries). A bank the agent paused on (``_voluntary_cooldown_banks``, written
every SAME_SPOT_MINE_LIMIT successful mines) or that reported depleted
(``depleted_banks``) and then ROAMED AWAY from is never re-queried, so its entry
never expires. Over a soak the agent rotates across many mining areas, so the
maps grew one entry per distinct bank ever touched — an unbounded leak.

``_voluntary_cooldown_banks`` is the worse case: the planner deadlock pass sweeps
``depleted_banks`` (and the bank CircuitBreaker) but never the voluntary map, so
nothing else ever reaps it. The fix sweeps every lapsed entry from both maps once
per ``_find_mineable_tile`` call, bounding them to banks on an ACTIVE cooldown.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from anima.skills.gathering.mine import (
    DEPLETED_COOLDOWN,
    VOLUNTARY_COOLDOWN_S,
    _bank_key,
    _find_mineable_tile,
)


def _make_ctx(
    depleted: dict[tuple[int, int], float],
    voluntary: dict[tuple[int, int], float],
    *,
    px: int = 2553,
    py: int = 496,
):
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.x = px
    ss.y = py
    ss.z = 0
    # A map reader that never reports a mineable tile, so _find_mineable_tile
    # runs the full scan (and the sweep) and returns None — we only care about
    # the pruning side effect here, not the returned tile.
    tile = MagicMock(statics=[])
    tile.land = MagicMock(graphic=0, z=0)
    ctx.map_reader = MagicMock()
    ctx.map_reader.get_tile.return_value = tile
    ctx.blackboard = {
        "depleted_banks": depleted,
        "_voluntary_cooldown_banks": voluntary,
    }
    return ctx


def test_expired_voluntary_cooldown_entries_are_pruned():
    """A voluntary-pause entry older than VOLUNTARY_COOLDOWN_S is dropped even
    when its bank is nowhere near the agent's current footing."""
    # Far-away bank the agent paused on then roamed away from — out of
    # MOVE_RADIUS, so _check_tile never queries it (no lazy self-prune).
    far_bank = _bank_key(9000, 9000)
    voluntary = {far_bank: 0.0}  # stamped long ago
    ctx = _make_ctx({}, voluntary)

    with patch("time.time", return_value=VOLUNTARY_COOLDOWN_S + 100.0):
        _find_mineable_tile(ctx)

    assert far_bank not in voluntary, (
        "stale _voluntary_cooldown_banks entry leaked — nothing else reaps it"
    )


def test_expired_depleted_bank_entries_are_pruned():
    """A depleted-bank entry past DEPLETED_COOLDOWN is dropped even when the
    agent has roamed far from that bank."""
    far_bank = _bank_key(9000, 9000)
    depleted = {far_bank: 0.0}
    ctx = _make_ctx(depleted, {})

    with patch("time.time", return_value=DEPLETED_COOLDOWN + 100.0):
        _find_mineable_tile(ctx)

    assert far_bank not in depleted


def test_fresh_cooldown_entries_are_kept():
    """Entries still inside their cooldown window survive the sweep."""
    now = VOLUNTARY_COOLDOWN_S + DEPLETED_COOLDOWN + 1000.0
    far_dep = _bank_key(9000, 9000)
    far_vol = _bank_key(9100, 9100)
    depleted = {far_dep: now - 1.0}  # 1s ago — still on cooldown
    voluntary = {far_vol: now - 1.0}
    ctx = _make_ctx(depleted, voluntary)

    with patch("time.time", return_value=now):
        _find_mineable_tile(ctx)

    assert far_dep in depleted
    assert far_vol in voluntary


def test_repeated_roaming_scans_do_not_grow_the_maps():
    """Simulate a soak: each scan parks a distinct far-away bank then rolls the
    clock past its cooldown. The maps must never accumulate stale keys."""
    depleted: dict[tuple[int, int], float] = {}
    voluntary: dict[tuple[int, int], float] = {}
    ctx = _make_ctx(depleted, voluntary)

    horizon = DEPLETED_COOLDOWN + VOLUNTARY_COOLDOWN_S + 10_000.0
    for i in range(100):
        # A new, far-apart bank gets parked in each map, stamped long enough ago
        # to be expired by the time the next scan runs.
        depleted[_bank_key(9000 + i * 64, 9000)] = 0.0
        voluntary[_bank_key(9000, 9000 + i * 64)] = 0.0
        with patch("time.time", return_value=horizon):
            _find_mineable_tile(ctx)

    assert depleted == {}, f"depleted_banks leaked: {depleted}"
    assert voluntary == {}, f"_voluntary_cooldown_banks leaked: {voluntary}"
