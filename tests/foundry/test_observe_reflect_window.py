"""observe.history()'s REFLECT-lite window must KEEP dead-end mutations.

Regression: history() sliced ``gs[-limit:]`` — pure global recency. Its whole
documented purpose is to stop the mutator re-walking dead ends ("three
independent meditation mutations that all earned zero skill gain"), but in a
long run newer unrelated cycles push those very failures out of the window, so
the mutator loses the memory the log exists to give it. The window must retain
dead ends (zero skill gain / low viability gate / aimed-but-missed) and only
backfill the rest with recency — while still reading oldest->newest.
"""
from __future__ import annotations

from foundry import observe
from foundry.kernel.archive import Genome


def _g(n: int, *, skill_rate: float = 5.0, gate: float = 1.0,
       target=None, cell=("GATHERING", 0)) -> Genome:
    return Genome(
        id=f"g_{n:05d}",
        parent=None,
        config={"target_cell": list(target) if target else None},
        eval={
            "fitness": skill_rate,
            "cell": list(cell),
            "breakdown": {"skill_gain_rate": skill_rate, "viability_gate": gate},
        },
        hypothesis=f"hyp{n}",
    )


def test_zero_skill_dead_end_is_kept_over_newer_healthy_cycles():
    # One early dead end (zero skill gain) followed by many healthy cycles that
    # would scroll it off a pure-recency window.
    dead = _g(1, skill_rate=0.0)
    healthy = [_g(n) for n in range(2, 20)]
    gs = [dead, *healthy]

    window = observe._reflect_window(gs, limit=5)
    ids = [g.id for g in window]

    assert len(window) == 5
    assert dead.id in ids, "the zero-skill dead end was evicted by recency"
    # pure recency would have dropped it:
    assert dead.id not in [g.id for g in gs[-5:]]
    # chronological order preserved (oldest -> newest by id)
    assert ids == sorted(ids)


def test_aimed_but_missed_dead_end_is_kept():
    miss = _g(1, target=("MAGIC", 2), cell=("GATHERING", 0))  # aimed != landed
    healthy = [_g(n) for n in range(2, 20)]
    window = observe._reflect_window([miss, *healthy], limit=4)
    assert miss.id in [g.id for g in window]


def test_low_gate_dead_end_is_kept():
    barely = _g(1, gate=0.1)
    healthy = [_g(n) for n in range(2, 20)]
    window = observe._reflect_window([barely, *healthy], limit=4)
    assert barely.id in [g.id for g in window]


def test_small_lineage_returns_everything_unchanged():
    gs = [_g(1), _g(2, skill_rate=0.0)]
    assert observe._reflect_window(gs, limit=12) == gs


def test_window_never_exceeds_limit_even_with_many_dead_ends():
    # More dead ends than the budget: keep the most RECENT dead ends, capped.
    dead = [_g(n, skill_rate=0.0) for n in range(1, 11)]
    window = observe._reflect_window(dead, limit=3)
    assert len(window) == 3
    # the newest dead ends survive the cap
    assert [g.id for g in window] == ["g_00008", "g_00009", "g_00010"]


def test_history_surfaces_an_old_dead_end_for_the_mutator():
    # End-to-end through history(): a stub archive with one old dead end and a
    # flood of newer healthy cycles. The rendered REFLECT log must mention it.
    dead = _g(1, skill_rate=0.0)
    healthy = [_g(n) for n in range(2, 30)]

    class _Arc:
        def all_genomes(self):
            return [dead, *healthy]

        def elites(self):
            return []

    text = observe.history(_Arc(), limit=6)
    assert "g_00001" in text, "old dead end missing from REFLECT log"
    assert "g_00001" not in "".join(g.id for g in [dead, *healthy][-6:][1:])
