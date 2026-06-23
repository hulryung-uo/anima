"""status.render() must not crash when per_seed_fitness carries a float-accepted
STRING (e.g. "50.0").

The kernel's reliability_score() coerces each per-seed entry with float() before
use, so a JSON-loaded genome can legitimately carry "50.0" in per_seed_fitness
and still promote into the grid. status.py used to feed those raw values to
round() directly, and round("50.0", 2) raises 'type str doesn't define
__round__' -- crashing the entire report on one stray string. The _round_seeds
helper now coerces to float first (the same contract the kernel uses).

These tests touch only what status.py DISPLAYS -- not the kernel promotion rule,
selection weighting, or reliability semantics.
"""
from __future__ import annotations

import re

from foundry import status
from foundry.kernel.archive import Archive, Genome


def _g(gid: str, cell: tuple, per_seed: list) -> Genome:
    # fitness mirrors what the kernel would compute; coerce for the mean so the
    # genome itself is valid even when per_seed holds strings.
    floats = [float(v) for v in per_seed]
    return Genome(
        id=gid,
        eval={
            "fitness": sum(floats) / len(floats),
            "cell": list(cell),
            "per_seed_fitness": list(per_seed),
        },
    )


def test_render_does_not_crash_on_stringified_per_seed(tmp_path):
    arc = Archive(tmp_path)
    # A float-accepted STRING survives the kernel's float() coercion in
    # reliability_score(); the report must render it, not raise __round__.
    arc.add(_g("g_00001", ("COMBAT", 0), ["50.0", "50.0"]))
    rendered = status.render(arc)  # must not raise
    assert "g_00001" in rendered
    # The coerced, rounded seed list is what gets displayed.
    assert "seeds=[50.0, 50.0]" in rendered


def test_render_mixed_string_and_float_seeds(tmp_path):
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("COMBAT", 0), ["40.0", 60.0]))
    rendered = status.render(arc)  # must not raise
    assert "seeds=[40.0, 60.0]" in rendered


def test_round_seeds_helper_coerces_and_rounds():
    assert status._round_seeds(["50.0", 50.0]) == [50.0, 50.0]
    assert status._round_seeds(["1.234", 2.345]) == [1.23, 2.35]


def test_round_seeds_helper_skips_non_numeric():
    # A genuinely malformed entry degrades one seed, not the whole report.
    assert status._round_seeds(["1.0", "nan?", None, 3.0]) == [1.0, 3.0]


def test_render_still_floats_normal_seeds(tmp_path):
    # Regression guard: ordinary float seeds render exactly as before.
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("COMBAT", 0), [12.5, 17.5]))
    rendered = status.render(arc)
    assert "seeds=[12.5, 17.5]" in rendered


def test_single_seed_genome_renders_no_seeds_clause(tmp_path):
    # len(per_seed) <= 1 -> no seeds clause, regardless of type.
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("COMBAT", 0), ["50.0"]))
    rendered = status.render(arc)
    line = next(l for l in rendered.splitlines() if "g_00001" in l)
    assert "seeds=" not in line
