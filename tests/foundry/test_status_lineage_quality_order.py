"""status.py's elites-lineage listing must rank by the variance-aware trust
signal, not the raw fitness mean.

Every other consumer of a cell elite -- select.choose_parent (parent quality),
reeval --elites (re-check trust order), and observe.history's "PROVEN recipes"
list (985ff44) -- ranks champions by ``_selection_quality = min(fitness,
reliability)`` (reliability = mean - lambda*pstdev). That signal discounts a
lucky high-variance elite (per_seed [400, 0] averages to 200 but its lower bound
is 0) and honors the human held-out corrections baked into ``eval.fitness``.

The operator-facing ``status`` lineage listing was the lone holdout: it sorted
``quality_elites`` by raw ``-g.fitness`` and printed the raw mean, so a coin-flip
elite (mean 200, reliability 0) headed the report as the run's apparent best --
the very recipe the rest of the pipeline ranks LAST. This is the exact
display-vs-trust inconsistency 985ff44 closed in observe.history. These tests pin
both halves of the fix: the trust-ordered sort and the trust number leading the
printed line. They do NOT touch selection weighting, the kernel promotion rule,
or the held-out / min(fitness, reliability) semantics.
"""
from __future__ import annotations

from foundry import status
from foundry.kernel.archive import Archive, Genome
from foundry.select import _selection_quality


def _g(gid: str, cell: tuple, per_seed: list[float]) -> Genome:
    return Genome(
        id=gid,
        eval={
            "fitness": sum(per_seed) / len(per_seed),
            "cell": list(cell),
            "per_seed_fitness": list(per_seed),
        },
    )


def _elites_section(rendered: str) -> str:
    lines = rendered.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("elites ("))
    return "\n".join(lines[start:])


def test_steady_elite_outranks_a_higher_mean_high_variance_elite(tmp_path):
    arc = Archive(tmp_path)
    # Steady: mean 50, pstdev 0 -> reliability 50 -> quality 50.
    steady = _g("g_00001", ("COMBAT", 0), [50.0, 50.0])
    # Lucky/volatile: mean 200 but pstdev 200 -> reliability 0 -> quality 0.
    lucky = _g("g_00002", ("MAGIC", 0), [400.0, 0.0])
    arc.add(steady)
    arc.add(lucky)

    # Sanity: the trust signal ranks the steady elite above the volatile one,
    # even though its raw mean is 4x lower.
    assert _selection_quality(steady) == 50.0
    assert _selection_quality(lucky) == 0.0

    section = _elites_section(status.render(arc))
    pos_steady = section.index("g_00001")
    pos_lucky = section.index("g_00002")
    # The steady (higher-quality) elite must appear ABOVE the volatile one.
    # Under the old raw-fitness sort the lucky mean-200 elite headed the list.
    assert pos_steady < pos_lucky, section


def test_lineage_line_leads_with_trust_quality_and_keeps_the_raw_mean(tmp_path):
    arc = Archive(tmp_path)
    lucky = _g("g_00002", ("MAGIC", 0), [400.0, 0.0])  # mean 200, quality 0
    arc.add(lucky)

    section = _elites_section(status.render(arc))
    line = next(ln for ln in section.splitlines() if "g_00002" in ln)
    # The displayed trust number (q…) leads, and the raw mean is still visible
    # (labelled) so no information is lost -- mirroring observe.history.
    assert "q" in line and "0.000" in line          # quality 0, not the 200 mean
    assert "mean" in line and "200.000" in line      # raw mean preserved (labelled)
    # The trust token precedes the raw mean on the line: the reader sees the
    # variance-aware quality FIRST, not the inflated mean.
    assert line.index("q") < line.index("mean") < line.index("200.000")
    # And the quality value the line LEADS with is the real trust signal (0),
    # which differs from the raw mean (200) -- proving they are distinct numbers.
    assert _selection_quality(lucky) == 0.0 and lucky.fitness == 200.0


def test_single_seed_elites_order_unchanged(tmp_path):
    # Legacy/single-seed genomes have no per-seed spread, so reliability falls
    # back to the point fitness and _selection_quality == fitness. The trust
    # ordering must then be identical to the old raw-fitness ordering -- the fix
    # only reorders genuinely high-variance elites.
    arc = Archive(tmp_path)
    arc.add(_g("g_00001", ("MAGIC", 0), [30.0]))
    arc.add(_g("g_00002", ("COMBAT", 1), [10.0]))
    arc.add(_g("g_00003", ("GATHERING", 2), [20.0]))

    section = _elites_section(status.render(arc))
    order = [section.index(gid) for gid in ("g_00001", "g_00003", "g_00002")]
    # Highest fitness (30) first, then 20, then 10 -- same as raw-fitness sort.
    assert order == sorted(order), section
