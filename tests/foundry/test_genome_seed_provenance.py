"""_genome_from must record the seed count that ACTUALLY backed a genome's
evidence (len(per_seed_fitness)), not the requested base rc.seeds.

per_seed_fitness is the authoritative evidence behind reliability and the
held-out correction semantics; its length diverges from rc.seeds whenever
adaptive top-up ran extra seeds, confirm-on-promotion pooled a second round, or
failed seeds were dropped. Recording rc.seeds mislabels such genomes (e.g. a
12-seed pooled genome recorded as `seeds: 3`) in status output, the mutator's
parent observation, and any reliability audit. This test pins the actual-count
behavior. It does NOT touch eval.fitness / reliability / min(fitness,reliability).
"""
from __future__ import annotations

from foundry.kernel.archive import Archive
from foundry.kernel.descriptor import Descriptor
from foundry.kernel.eval import EvalResult
from foundry.kernel.fitness import FitnessBreakdown
from foundry.orchestrator import RunConfig, _genome_from


def _result(per_seed: list[float]) -> EvalResult:
    mean = sum(per_seed) / len(per_seed) if per_seed else 0.0
    return EvalResult(
        ok=True,
        fitness=FitnessBreakdown(total=mean),
        descriptor=Descriptor(profession_focus="GATHERING"),
        per_seed_fitness=list(per_seed),
    )


def test_seed_count_reflects_actual_evidence_after_topup(tmp_path):
    arc = Archive(root=tmp_path)
    rc = RunConfig(seeds=3)  # requested base
    # adaptive top-up / confirm-pooling produced 6 backing seeds
    res = _result([10.0, 10.0, 10.0, 12.0, 12.0, 12.0])
    g = _genome_from(arc, res, parent=None, code_ref="HEAD", hypothesis="x", rc=rc)
    assert g.config["seeds"] == 6, "must record actual seed count, not rc.seeds=3"
    # the authoritative evidence list itself is unchanged
    assert g.eval["per_seed_fitness"] == [10.0, 10.0, 10.0, 12.0, 12.0, 12.0]
    assert g.config["seeds"] == len(g.eval["per_seed_fitness"])


def test_seed_count_reflects_dropped_failed_seeds(tmp_path):
    arc = Archive(root=tmp_path)
    rc = RunConfig(seeds=4)
    # 4 requested but only 2 seeds survived (2 failed and were dropped)
    res = _result([50.0, 54.0])
    g = _genome_from(arc, res, parent=None, code_ref="HEAD", hypothesis="x", rc=rc)
    assert g.config["seeds"] == 2
    assert g.config["seeds"] == len(g.eval["per_seed_fitness"])


def test_single_seed_matches_request(tmp_path):
    arc = Archive(root=tmp_path)
    rc = RunConfig(seeds=1)
    res = _result([42.0])
    g = _genome_from(arc, res, parent=None, code_ref="HEAD", hypothesis="x", rc=rc)
    assert g.config["seeds"] == 1


def test_empty_per_seed_falls_back_to_request(tmp_path):
    # Defensive: a result with no per-seed evidence falls back to rc.seeds so
    # the field is never 0 / misleadingly "no seeds".
    arc = Archive(root=tmp_path)
    rc = RunConfig(seeds=3)
    res = EvalResult(ok=True, fitness=FitnessBreakdown(total=0.0),
                     descriptor=Descriptor(), per_seed_fitness=[])
    g = _genome_from(arc, res, parent=None, code_ref="HEAD", hypothesis="x", rc=rc)
    assert g.config["seeds"] == 3


def test_fitness_and_evidence_untouched(tmp_path):
    # Guard: the provenance fix changes ONLY config.seeds — the authoritative
    # fitness scalar and per_seed evidence (which drive reliability / held-out
    # min(fitness,reliability)) are stored verbatim.
    arc = Archive(root=tmp_path)
    rc = RunConfig(seeds=2)
    res = _result([100.0, 0.0])  # mean 50, high variance
    g = _genome_from(arc, res, parent=None, code_ref="HEAD", hypothesis="x", rc=rc)
    assert g.eval["fitness"] == 50.0
    assert g.eval["per_seed_fitness"] == [100.0, 0.0]
    assert g.config["seeds"] == 2
