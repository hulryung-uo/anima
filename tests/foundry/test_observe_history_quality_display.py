"""observe.history() must DISPLAY the variance-aware selection quality it RANKS by.

Regression: the "PROVEN recipes (mine these for tricks)" list was migrated to
sort by ``_selection_quality = min(fitness, reliability)`` (reliability =
mean − λ·pstdev) so a lucky high-variance or human-demoted genome can't head the
list. But the printed number stayed the raw fitness mean (``e.fitness``) — the
number the LLM mutator actually reads. So a coin-flip elite (per_seed [400, 0],
mean 200, quality 0) still advertised itself as a "200.00 proven recipe" even
though it correctly sorted last, telling the mutator to mine a recipe the rest of
the pipeline distrusts. The displayed number must match the ranking signal.
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


def _recipe_line(text: str, gid: str) -> str:
    # The recipe block is rendered first; its lines start with "- " and contain
    # the genome id but never the "parent " token (that belongs to the lower
    # "Prior mutations" block).
    for ln in text.splitlines():
        if ln.startswith("- ") and gid in ln and "parent" not in ln:
            return ln
    raise AssertionError(f"no recipe line for {gid} in:\n{text}")


def test_lucky_high_variance_recipe_displays_its_low_quality_not_the_mean():
    # per_seed [400, 0]: mean 200, reliability 0 → _selection_quality 0.
    lucky = _g("g_00001", ("MAGIC", 0), [400.0, 0.0])
    steady = _g("g_00002", ("GATHERING", 0), [150.0, 150.0])
    assert _selection_quality(lucky) == 0.0
    assert _selection_quality(steady) == 150.0

    text = observe.history(_Arc([lucky, steady]))
    line = _recipe_line(text, "g_00001")

    # The headline trust number is the quality (0.00), NOT a bare 200.00 that
    # would mis-sell a coin-flip elite as a top recipe to the LLM mutator.
    assert "q0.00" in line
    # The raw mean is still available, but only behind the explicit "mean" label
    # so it cannot be mistaken for the recipe's trustworthy fitness.
    assert "mean 200.00" in line
    assert "200.00 (g_00001)" not in line  # the old, misleading rendering


def test_displayed_quality_matches_the_ranking_key_for_every_recipe():
    elites = [
        _g("g_00001", ("MAGIC", 0), [300.0, 0.0]),        # quality min(150, -150) = -150
        _g("g_00002", ("GATHERING", 0), [120.0, 120.0]),  # quality 120
        _g("g_00003", ("COMBAT", 0), [80.0, 60.0]),        # quality min(70, 60) = 60
    ]
    text = observe.history(_Arc(elites))
    for e in elites:
        line = _recipe_line(text, e.id)
        assert f"q{_selection_quality(e):.2f}" in line, (
            f"{e.id} must display its ranking-key quality, got: {line}")
        assert f"mean {e.fitness:.2f}" in line
