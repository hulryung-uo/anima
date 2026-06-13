"""Kernel tests — parse / fitness / descriptor / archive, against real samples.

These exercise the Foundry kernel offline (no server, no anima import), using the
recorded sample trajectories as ground truth. Run: uv run pytest tests/foundry/ -q
"""

from __future__ import annotations

import glob
import math
from pathlib import Path

import pytest

from foundry.kernel import uoconst
from foundry.kernel.archive import Archive, Genome
from foundry.kernel.descriptor import compute_descriptor
from foundry.kernel.fitness import MOBILITY_RATE_CAP, WB_EXPLORE, compute_fitness
from foundry.kernel.trajectory import TrajectorySummary, parse_file

REPO = Path(__file__).resolve().parents[2]
SAMPLES = sorted(glob.glob(str(REPO / "data" / "trajectories" / "demo-*.jsonl")))


@pytest.fixture(scope="module")
def summaries() -> dict[str, TrajectorySummary]:
    assert SAMPLES, "no sample trajectories found"
    return {Path(f).name: parse_file(f) for f in SAMPLES}


def test_samples_parse_clean(summaries):
    for name, s in summaries.items():
        assert s.parse_errors == 0, f"{name} had parse errors"
        assert s.line_count > 0


def test_kernel_does_not_import_anima():
    import sys

    import foundry.kernel.archive as a
    import foundry.kernel.descriptor as d
    import foundry.kernel.fitness as f
    import foundry.kernel.trajectory as t
    # No anima.* module should be pulled in by importing the kernel.
    for mod in (t, f, d, a):
        assert not any(k.startswith("anima") for k in vars(mod)
                       if isinstance(vars(mod).get(k), type(sys))), \
            f"{mod.__name__} imported an anima module"


def test_fitness_nonnegative_and_bounded(summaries):
    for name, s in summaries.items():
        fb = compute_fitness(s)
        assert fb.total >= 0.0
        assert 0.0 <= fb.viability_gate <= 1.0
        assert 0.0 <= fb.alive_fraction <= 1.0
        assert not math.isnan(fb.total)


def test_idle_session_gated_to_zero(summaries):
    # The shortest/idlest demos should score ~0 via the viability gate.
    shortest = min(summaries.values(), key=lambda s: s.total_actions)
    fb = compute_fitness(shortest)
    assert fb.total < 1.0


def test_mining_sample_is_gathering(summaries):
    # demo-124740 contains a real Mining +0.2 gain.
    target = next((s for n, s in summaries.items() if "124740" in n), None)
    if target is None:
        pytest.skip("mining sample not present")
    d = compute_descriptor(target)
    assert d.profession_focus == uoconst.GATHERING
    assert len(d.cell) == 2  # Phase-0 active grid: (profession, sociability)


def test_mobility_cap_bounds_behavior_bonus():
    s = TrajectorySummary(path="synthetic", start_ts=0.0, end_ts=60.0)  # 1 minute
    # fabricate massive exploration: 1000 unique regions
    s.positions = [(0.0, x * 8, 0) for x in range(1000)]
    s.action_counts = {"move": 1000}
    fb = compute_fitness(s)
    # exploration contribution must be capped, not 1000-regions-per-minute huge
    assert fb.behavior_bonus <= WB_EXPLORE * MOBILITY_RATE_CAP + 1e-6


def test_archive_insertion_and_persistence(tmp_path):
    arc = Archive(tmp_path / "arc")

    def mk(gid, cell, fit):
        return Genome(id=gid, eval={"fitness": fit, "cell": list(cell)})

    assert arc.add(mk("g_00001", ("GATHERING", 0), 1.0)).status == "filled"
    assert arc.add(mk("g_00002", ("GATHERING", 0), 0.5)).status == "rejected"
    assert arc.add(mk("g_00003", ("GATHERING", 0), 2.0)).status == "improved"
    assert arc.add(mk("g_00004", ("COMBAT", 1), 0.1)).status == "filled"

    assert arc.filled_cells() == 2
    assert arc.get_elite(("GATHERING", 0)).id == "g_00003"
    assert abs(arc.qd_score() - 2.1) < 1e-9
    assert arc.best().id == "g_00003"

    # grid round-trips through disk
    arc2 = Archive(tmp_path / "arc")
    assert arc2.grid == arc.grid
    assert arc2.get_elite(("GATHERING", 0)).fitness == 2.0


def test_promotion_uses_reliability_not_mean(tmp_path):
    """A volatile challenger with a higher MEAN must not displace a steadier
    incumbent: promotion compares mean − λ·pstdev, not the raw mean."""
    arc = Archive(tmp_path / "arc")

    def mk(gid, cell, per_seed):
        import statistics
        return Genome(id=gid, eval={
            "fitness": statistics.fmean(per_seed),
            "cell": list(cell),
            "per_seed_fitness": per_seed,
        })

    # steady incumbent: mean 50, no spread -> reliability 50
    assert arc.add(mk("g_1", ("COMBAT", 0), [50.0, 50.0, 50.0])).status == "filled"
    # volatile challenger: higher MEAN 52 but huge spread (a lucky run)
    # mean 52, pstdev ~26 -> reliability ~26 < 50 -> REJECTED despite higher mean
    r = arc.add(mk("g_2", ("COMBAT", 0), [88.0, 50.0, 18.0]))
    assert r.status == "rejected"
    assert arc.get_elite(("COMBAT", 0)).id == "g_1"
    # a genuinely better, steady challenger DOES promote
    r2 = arc.add(mk("g_3", ("COMBAT", 0), [60.0, 58.0, 62.0]))
    assert r2.status == "improved"
    assert arc.get_elite(("COMBAT", 0)).id == "g_3"


def test_reliability_falls_back_to_point_estimate(tmp_path):
    """Legacy/single-seed genomes (no per_seed_fitness) compare by raw fitness."""
    from foundry.kernel.archive import reliability_score
    assert reliability_score([], 7.0) == 7.0
    assert reliability_score([7.0], 7.0) == 7.0


def test_use_skill_counts_as_action():
    """0x12 (UseSkill/CastSpell) must count toward liveness/actions.

    Regression: skill-only professions (Hiding, Meditation grinds) read
    as frozen — gate collapse — because 0x12 was missing from
    ACTION_GROUP, and their sociability inflated to speech/speech.
    """
    from foundry.kernel import uoconst

    assert uoconst.ACTION_GROUP.get(0x12) == "skill"
    # The use→target flow must not double-count: target reply excluded.
    assert uoconst.CS_TARGET not in uoconst.ACTION_GROUP
