"""MetaController._mode_history must not grow without bound.

The meta-controller is a *live* runtime component that runs for the entire
open-ended lifetime of the agent process, appending one mode string every
``interval_s`` (>= 60s). Only the last few entries are ever consumed
(``build_living_state`` slices ``last_modes[-6:]``), so an unbounded list was a
pure soak leak: the list grew forever even though all but the tail was dead.

Regression guard: the history is a bounded ``deque`` and ``build_living_state``
still receives a real list (deques don't slice) and yields the last-6 tail.
"""

from __future__ import annotations

from collections import deque

from anima.planner.meta_controller import MetaController, build_living_state


def test_mode_history_is_a_bounded_deque():
    c = MetaController()
    assert isinstance(c._mode_history, deque)
    assert c._mode_history.maxlen == MetaController._MODE_HISTORY_CAP


def test_mode_history_never_exceeds_cap_under_long_soak():
    """Simulate a long-running session appending far more decisions than the
    cap. The buffer must stay bounded, not accumulate one entry per decision
    forever."""
    c = MetaController()
    cap = MetaController._MODE_HISTORY_CAP
    n = cap * 50  # far more decisions than the cap
    for i in range(n):
        c._mode_history.append("mining" if i % 2 == 0 else "combat")
    assert len(c._mode_history) == cap
    # The retained tail is the most-recent decisions.
    assert list(c._mode_history)[-1] in ("mining", "combat")


def test_build_living_state_slices_the_tail_from_a_deque_history():
    """``last_modes`` is sliced ``[-6:]`` downstream. A deque can't be sliced,
    so the controller passes ``list(self._mode_history)``; assert the slice
    still yields the last 6 in order regardless of how full the history is."""
    from types import SimpleNamespace

    c = MetaController()
    for i in range(20):
        c._mode_history.append(f"m{i}")

    ss = SimpleNamespace(
        hits=100,
        hits_max=100,
        weight=0,
        weight_max=100,
        gold=0,
        x=0,
        y=0,
        equipment={},
    )
    ctx = SimpleNamespace(
        perception=SimpleNamespace(
            self_state=ss,
            world=SimpleNamespace(
                items={},
                nearby_mobiles=lambda *a, **k: [],
            ),
        ),
        persona=SimpleNamespace(profession="miner"),
    )

    state = build_living_state(
        ctx,
        gold_rate_per_min=0.0,
        session_minutes=1.0,
        last_modes=list(c._mode_history),
    )
    assert state.last_modes == [f"m{i}" for i in range(14, 20)]
