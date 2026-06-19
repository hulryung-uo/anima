"""Arrival-predicate precision for go_to (exact vs adjacent)."""

from __future__ import annotations

from anima.action.movement import _arrived


def test_exact_requires_the_exact_tile():
    # On the tile -> arrived.
    assert _arrived(10, 10, 10, 10, exact=True) is True
    # One tile short (cardinal) -> NOT arrived when exact.
    assert _arrived(10, 10, 11, 10, exact=True) is False
    # One tile short (diagonal) -> NOT arrived when exact.
    assert _arrived(10, 10, 11, 11, exact=True) is False


def test_adjacent_accepts_one_tile_out():
    # On the tile -> arrived.
    assert _arrived(10, 10, 10, 10, exact=False) is True
    # One tile out (cardinal or diagonal) counts as arrived.
    assert _arrived(10, 10, 11, 10, exact=False) is True
    assert _arrived(10, 10, 11, 11, exact=False) is True
    # Two tiles out -> not arrived.
    assert _arrived(10, 10, 12, 10, exact=False) is False


def test_loop_and_fallback_predicates_agree():
    """The per-step check and the max_steps fallback must use one rule.

    Regression: the fallback return was a hard-coded ``<= 1`` Chebyshev
    check that ignored ``exact``. A go_to(exact=True) that ran out of steps
    exactly one tile short therefore returned True (false arrival), letting
    callers like the blacksmith craft routine believe they reached the
    forge tile when they had not.
    """
    tx, ty = 100, 100
    # The historical buggy fallback: within-1-tile always True.
    buggy_fallback = lambda sx, sy: max(abs(tx - sx), abs(ty - sy)) <= 1

    # One tile short.
    sx, sy = 100, 99
    # Adjacent mode: both old and new agree (arrived).
    assert _arrived(sx, sy, tx, ty, exact=False) == buggy_fallback(sx, sy) is True
    # Exact mode: the new predicate diverges from the old buggy fallback.
    assert _arrived(sx, sy, tx, ty, exact=True) is False
    assert buggy_fallback(sx, sy) is True  # demonstrates the old false-positive


def test_exact_arrival_is_per_step_consistent():
    # Whatever the per-step check accepts at the destination, the fallback
    # accepts the same — verified by sharing _arrived for both.
    for exact in (True, False):
        for sx, sy in [(5, 5), (5, 6), (6, 6), (7, 5)]:
            assert _arrived(sx, sy, 5, 5, exact) == (
                max(abs(5 - sx), abs(5 - sy)) <= (0 if exact else 1)
            )
