"""A would-be promotion whose confirm-on-promotion round can't corroborate the
cell must NOT crown that cell on the lone lucky first round.

--confirm-promotions exists so a single lucky run can't crown a volatile genome:
a would-be winner is re-eval'd and the rounds are pooled before promotion. But
when the confirmation FAILS (all seeds error) or DRIFTS to a different cell,
``_pool_confirmation`` correctly refuses to pool and returns the first-round
object unchanged. The orchestrator used to then hand that un-corroborated
first round straight to ``arc.add`` — so the exact lucky single run the feature
exists to reject crowned the cell anyway.

The fix flags an un-corroborated would-be promotion (``_pool_confirmation``
returned the SAME object) and lands it via ``_archive_genome(..., promote=False)``
which persists the genome for lineage but skips the grid promotion. These tests
pin both halves: the identity signal and the persist-without-promote landing.
They do NOT touch the kernel promotion rule or the held-out semantics.
"""
from __future__ import annotations

from foundry.kernel.archive import Archive, Genome
from foundry.kernel.descriptor import Descriptor
from foundry.kernel.eval import EvalResult
from foundry.kernel.fitness import FitnessBreakdown
from foundry.orchestrator import _archive_genome, _pool_confirmation


def _result(per_seed: list[float], prof: str, soc_bin: int) -> EvalResult:
    mean = sum(per_seed) / len(per_seed) if per_seed else 0.0
    return EvalResult(
        ok=True,
        fitness=FitnessBreakdown(total=mean),
        descriptor=Descriptor(profession_focus=prof, sociability_bin=soc_bin),
        per_seed_fitness=list(per_seed),
    )


def _genome(gid: str, cell: tuple, per_seed: list[float]) -> Genome:
    mean = sum(per_seed) / len(per_seed) if per_seed else 0.0
    return Genome(
        id=gid,
        code_ref="deadbeef",
        eval={
            "fitness": mean,
            "cell": list(cell),
            "per_seed_fitness": list(per_seed),
        },
    )


# --- the identity signal the orchestrator keys off ------------------------

def test_failed_confirm_keeps_first_round_object():
    first = _result([200.0], "COMBAT", 0)  # one lucky run
    confirm = EvalResult(ok=False, error="all seeds failed")
    out = _pool_confirmation(first, confirm)
    # Same object back == "confirmation did not corroborate" → orchestrator
    # must treat this as an UN-corroborated would-be promotion.
    assert out is first


def test_drifted_confirm_keeps_first_round_object():
    first = _result([200.0], "COMBAT", 0)
    confirm = _result([180.0], "COMBAT", 1)  # drifted to a neighbour cell
    out = _pool_confirmation(first, confirm)
    assert out is first


def test_corroborated_confirm_returns_a_fresh_object():
    first = _result([200.0, 200.0], "COMBAT", 0)
    confirm = _result([190.0, 190.0], "COMBAT", 0)
    out = _pool_confirmation(first, confirm)
    assert out is not first  # pooled → orchestrator promotes normally
    assert out.per_seed_fitness == [200.0, 200.0, 190.0, 190.0]


# --- the landing decision --------------------------------------------------

def test_uncorroborated_winner_is_persisted_but_not_promoted(tmp_path):
    arc = Archive(tmp_path / "archive")
    cell = ("COMBAT", 0)
    # A steady incumbent already holds the cell.
    incumbent = _genome("g_00001", cell, [40.0, 40.0, 40.0])
    assert arc.add(incumbent).status == "filled"

    # A lucky single-round candidate whose reliability (== its lone score, 200)
    # would beat the incumbent's (40) — exactly a would-be promotion. Its
    # confirmation could not corroborate the cell, so it must NOT crown it.
    lucky = _genome("g_00002", cell, [200.0])
    assert lucky.reliability > incumbent.reliability  # would win on raw evidence

    r = _archive_genome(arc, lucky, promote=False)

    assert r is None, "an un-corroborated would-be promotion is not inserted"
    # Lineage preserved: the genome is on disk and loadable by id.
    assert arc.get("g_00002") is not None
    # The cell still belongs to the steady incumbent — the lucky run did NOT win.
    elite = arc.get_elite(cell)
    assert elite is not None and elite.id == "g_00001", (
        "lucky single round must not crown the cell when confirm failed"
    )


def test_corroborated_winner_still_promotes(tmp_path):
    arc = Archive(tmp_path / "archive")
    cell = ("COMBAT", 0)
    incumbent = _genome("g_00001", cell, [40.0, 40.0, 40.0])
    arc.add(incumbent)

    # A pooled (corroborated) winner lands via the normal promotion path.
    winner = _genome("g_00002", cell, [120.0, 120.0, 120.0, 110.0])
    r = _archive_genome(arc, winner, promote=True)

    assert r is not None and r.status == "improved"
    elite = arc.get_elite(cell)
    assert elite is not None and elite.id == "g_00002"
