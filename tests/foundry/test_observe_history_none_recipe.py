"""observe.history()'s "PROVEN recipes" list must exclude the NONE-fallback row.

Regression: the elite block captioned "the PROVEN recipes (mine these for
tricks)" iterated the kernel's raw ``archive.elites()``, which still records a
NONE-fallback-row elite — a degenerate agent that gained no trade skill and
banked a high-variance NONE score (the scoring artifact the held-out
corrections flag, e.g. g_00118). Every OTHER mutator-facing / display consumer
already drops it: ``select.TARGET_PROFESSIONS`` never aims a mutation there,
``observe._archive_context_lines`` counts only targetable cells, and
``status.render`` tables only active cells. Leaving it in the recipe list tells
the LLM mutator to mine a recipe that produced no profession progress — the one
thing the rest of the pipeline deliberately distrusts. The list must describe
the same targetable universe.
"""
from __future__ import annotations

from foundry import observe
from foundry.kernel import uoconst
from foundry.kernel.archive import Genome


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


def _recipe_block(text: str) -> str:
    """The lines of the first ('PROVEN recipes') section only."""
    out: list[str] = []
    for ln in text.splitlines():
        if ln.startswith("## Prior mutations"):
            break
        out.append(ln)
    return "\n".join(out)


def test_none_fallback_elite_is_not_listed_as_a_proven_recipe():
    none_elite = _g("g_00118", (uoconst.NONE, 0), [80.0, 0.0])  # volatile NONE
    real_elite = _g("g_00200", ("GATHERING", 0), [40.0, 42.0])
    text = observe.history(_Arc([none_elite, real_elite]))
    recipes = _recipe_block(text)
    assert "g_00200" in recipes, "the real trade elite must still be a recipe"
    assert "g_00118" not in recipes, (
        "the NONE-fallback elite must NOT be offered to the mutator as a "
        "PROVEN recipe — the rest of the pipeline excludes it"
    )


def test_targetable_elites_all_survive_the_filter():
    elites = [
        _g("g_00001", ("MAGIC", 0), [50.0, 50.0]),
        _g("g_00002", ("GATHERING", 0), [40.0, 40.0]),
        _g("g_00003", (uoconst.NONE, 1), [30.0, 30.0]),  # dropped
    ]
    recipes = _recipe_block(observe.history(_Arc(elites)))
    assert "g_00001" in recipes
    assert "g_00002" in recipes
    assert "g_00003" not in recipes


def test_grid_of_only_none_elites_yields_an_empty_recipe_list():
    elites = [_g("g_00010", (uoconst.NONE, 0), [10.0, 10.0])]
    text = observe.history(_Arc(elites))
    recipes = _recipe_block(text)
    # The header is still present, but no recipe bullet should be rendered.
    assert "PROVEN recipes" in recipes
    assert "g_00010" not in recipes
