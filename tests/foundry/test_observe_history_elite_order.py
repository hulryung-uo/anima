"""observe.history() ranks its "PROVEN recipes" elite list by the variance-aware
selection quality select.py / the kernel actually use — not the raw fitness mean.

Regression: the elite block captioned "the PROVEN recipes (mine these for tricks)"
sorted by ``-g.fitness``. select.py was deliberately migrated to rank by
``_selection_quality = min(fitness, reliability)`` (reliability = mean − λ·pstdev)
precisely because the raw mean over-weights lucky high-variance genomes AND
bypasses the human held-out corrections baked into ``eval.fitness``. With the raw
sort, a volatile/demoted genome floats to the TOP of the list the LLM mutator is
told to mine for tricks — contradicting the held-out-correction invariant the
rest of the pipeline guards. The ordering must honor the same signal.
"""
from __future__ import annotations

from foundry import observe
from foundry.kernel.archive import Genome
from foundry.select import _selection_quality


def _g(gid: str, cell: tuple, per_seed: list[float]) -> Genome:
    mean = sum(per_seed) / len(per_seed) if per_seed else 0.0
    return Genome(
        id=gid,
        parent=None,
        config={"target_cell": None},
        eval={
            "fitness": mean,
            "cell": list(cell),
            "per_seed_fitness": list(per_seed),
            "breakdown": {"skill_gain_rate": 5.0, "viability_gate": 1.0},
        },
        hypothesis=f"recipe-{gid}",
    )


class _Arc:
    def __init__(self, elites: list[Genome]) -> None:
        self._elites = elites

    def elites(self) -> list[Genome]:
        return list(self._elites)

    def all_genomes(self) -> list[Genome]:
        return list(self._elites)


def test_lucky_high_variance_elite_does_not_top_the_recipe_list():
    # A: higher raw mean (200) but a 400/0 split → reliability 0, so its
    #    selection quality min(200, 0) = 0 (a coin-flip, not a proven recipe).
    # B: lower raw mean (150) but rock-steady → selection quality 150.
    lucky = _g("g_00001", ("MAGIC", 0), [400.0, 0.0])
    steady = _g("g_00002", ("GATHERING", 0), [150.0, 150.0])

    # Sanity: the bug's ordering signal (raw fitness) and the correct one disagree.
    assert lucky.fitness > steady.fitness
    assert _selection_quality(steady) > _selection_quality(lucky)

    text = observe.history(_Arc([lucky, steady]))
    recipe_lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
    # Both elites appear in the "PROVEN recipes" block (it is rendered first).
    ids_in_order = [ln for ln in recipe_lines if "g_0000" in ln]
    pos_steady = next(i for i, ln in enumerate(ids_in_order) if "g_00002" in ln)
    pos_lucky = next(i for i, ln in enumerate(ids_in_order) if "g_00001" in ln)
    assert pos_steady < pos_lucky, (
        "the steady high-quality recipe must rank above the lucky high-variance "
        "one in the list the mutator is told to mine"
    )


def test_recipe_order_matches_selection_quality_for_many_elites():
    elites = [
        _g("g_00001", ("MAGIC", 0), [300.0, 0.0]),       # quality min(150, -150)=-150
        _g("g_00002", ("GATHERING", 0), [120.0, 120.0]),  # quality 120
        _g("g_00003", ("COMBAT", 0), [80.0, 60.0]),        # quality min(70, 60)=60
    ]
    text = observe.history(_Arc(elites))
    recipe_lines = [ln for ln in text.splitlines()
                    if ln.startswith("- ") and "g_0000" in ln
                    and "parent" not in ln]
    rendered = [next(g.id for g in elites if g.id in ln) for ln in recipe_lines]
    expected = [g.id for g in sorted(elites, key=lambda g: -_selection_quality(g))]
    assert rendered == expected
