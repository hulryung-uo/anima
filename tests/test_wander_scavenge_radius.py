"""Tests for _WanderAndScavenge target selection.

The deadlock-recovery town scavenge must stay "near town": the docstring
promises ±5-15 tile hops covering town items, but the original code added a
fresh random offset to the *current* position each step, so the offsets
compounded and the agent drifted hundreds of tiles into wilderness. The fix
anchors every stop to the start position and clamps it to WANDER_RADIUS, the
same "orbit the anchor, don't drift away" discipline WanderForCombat uses.
"""
import random
from types import SimpleNamespace

from anima.planner.planner import _WanderAndScavenge as W


def test_single_hop_stays_in_range():
    rng = random.Random(0)
    sx, sy = 100, 100
    for _ in range(200):
        tx, ty = W._next_target(sx, sy, sx, sy, rng)
        assert max(abs(tx - sx), abs(ty - sy)) <= W.WANDER_RADIUS


def test_hop_actually_moves():
    # A real wander step, not a no-op: the target differs from the anchor.
    rng = random.Random(1)
    sx, sy = 100, 100
    moved = False
    for _ in range(50):
        tx, ty = W._next_target(sx, sy, sx, sy, rng)
        if (tx, ty) != (sx, sy):
            moved = True
    assert moved


def test_far_from_anchor_is_clamped_back_inward():
    # If the agent has wandered to the radius edge, the next target is pinned
    # to the boundary — it can never push further out (no compounding drift).
    rng = random.Random(2)
    sx, sy = 100, 100
    cur_x = sx + W.WANDER_RADIUS  # sitting on the east edge
    cur_y = sy
    for _ in range(200):
        tx, ty = W._next_target(sx, sy, cur_x, cur_y, rng)
        assert max(abs(tx - sx), abs(ty - sy)) <= W.WANDER_RADIUS


def test_no_unbounded_drift_over_a_full_sweep():
    # Simulate WANDER_STEPS hops where each hop's result becomes the next
    # current position (as the real run loop does). The agent must remain
    # within the radius the whole time — the regression this guards against
    # is the old cur-relative target letting the walk march away unbounded.
    rng = random.Random(3)
    sx, sy = 500, 500
    cur_x, cur_y = sx, sy
    max_seen = 0
    for _ in range(W.WANDER_STEPS):
        cur_x, cur_y = W._next_target(sx, sy, cur_x, cur_y, rng)
        max_seen = max(max_seen, abs(cur_x - sx), abs(cur_y - sy))
    assert max_seen <= W.WANDER_RADIUS
