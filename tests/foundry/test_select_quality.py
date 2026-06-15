"""Backlog rank 2 (corrected): parent selection must be variance-aware AND
honor human held-out corrections.

reliability (mean − λ·pstdev, recomputed from per_seed_fitness) is right for
normal genomes, but it bypasses the authoritative eval.fitness scalar that the
human grid-correction tool overwrote for the ruler-inflation genomes — so
reliability alone RE-INFLATES exactly the genomes a human demoted (g_00052:
held-out 3.06 → reliability 174.9). _selection_quality = min(fitness,
reliability) honors the correction while staying conservative for normal genomes.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from foundry.kernel.archive import Genome
from foundry.select import _selection_quality

ARCHIVE = Path(__file__).resolve().parents[2] / "foundry" / "archive" / "genomes"


def test_normal_genome_uses_conservative_bound():
    # reliability <= fitness for a normal multi-seed genome → min() = reliability
    g = SimpleNamespace(fitness=100.0, reliability=80.0)
    assert _selection_quality(g) == 80.0


def test_held_out_correction_not_reinflated():
    # corrected genome: held-out fitness LOW, reliability (from inflated
    # per_seed) HIGH → min() must pick the held-out value, not re-inflate.
    g = SimpleNamespace(fitness=3.06, reliability=174.92)
    assert _selection_quality(g) == 3.06


@pytest.mark.parametrize("gid", ["g_00049", "g_00051", "g_00042", "g_00050", "g_00052"])
def test_real_corrected_genomes_honor_held_out(gid):
    """Integration: the 5 ruler-inflation genomes carry held_out_correction;
    selection quality must equal their held-out fitness, never the inflated
    reliability recomputed from preserved per_seed evidence."""
    path = ARCHIVE / f"{gid}.json"
    if not path.exists():
        pytest.skip(f"{gid} not in archive")
    d = json.loads(path.read_text())
    assert d["eval"].get("held_out_correction"), f"{gid} should be a corrected genome"
    g = Genome(**{k: d[k] for k in ("id", "parent", "code_ref", "config", "eval", "hypothesis", "ts") if k in d})
    # The anti-inflation guarantee: selection never weights a corrected genome
    # ABOVE its human held-out value, even though reliability (from the preserved
    # inflated per_seed evidence) often sits far higher.
    assert _selection_quality(g) <= g.fitness + 1e-9
