"""A confirm-on-promotion round that can't corroborate must still FILL an EMPTY
cell — there is no incumbent to protect from the optimizer's curse.

``--confirm-promotions`` exists to kill the optimizer's curse (archive.py): a
single LUCKY run displacing a STEADIER elite that already holds the cell. When
the confirm round fails or drifts off-cell, ``_pool_confirmation`` refuses to
pool and the would-be promotion is blocked so it can't crown the cell on the
lone lucky first round.

But that protection only makes sense when there is an incumbent to displace. For
an EMPTY cell there is no curse to kill: blocking the promotion just discards a
valid first-round eval (and the confirm eval already spent), leaves the cell
empty (0 QD-score), and re-queues it for future EXPLORE cycles that re-run the
same work. ``_confirm_uncorroborated`` therefore blocks only when an incumbent
existed; against an empty cell it fills from the first round exactly as a
no-confirm run would.

These tests pin the pure decision helper across all four (pooled?, incumbent?)
combinations and the end-to-end landing (empty cell fills, occupied cell stays
protected). They do NOT touch the kernel promotion rule or held-out semantics.
"""
from __future__ import annotations

from foundry.kernel.archive import Archive, Genome
from foundry.kernel.descriptor import Descriptor
from foundry.kernel.eval import EvalResult
from foundry.kernel.fitness import FitnessBreakdown
from foundry.orchestrator import (
    _archive_genome,
    _confirm_uncorroborated,
    _pool_confirmation,
)


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


# --- the pure decision: block only when displacing an incumbent -------------

def test_uncorroborated_against_empty_cell_does_not_block():
    # Confirm failed/drifted (pooled is the same first-round object) but the cell
    # was EMPTY (no incumbent) → fill it from the first round, do NOT block.
    first = _result([200.0], "COMBAT", 0)
    confirm = EvalResult(ok=False, error="all seeds failed")
    pooled = _pool_confirmation(first, confirm)
    assert pooled is first  # not corroborated
    assert _confirm_uncorroborated(first, pooled, had_incumbent=False) is False


def test_uncorroborated_against_incumbent_blocks():
    # Same un-pooled confirm, but an incumbent holds the cell → the lucky single
    # round must NOT displace it: block the promotion (optimizer's curse).
    first = _result([200.0], "COMBAT", 0)
    confirm = _result([180.0], "COMBAT", 1)  # drifted off-cell
    pooled = _pool_confirmation(first, confirm)
    assert pooled is first
    assert _confirm_uncorroborated(first, pooled, had_incumbent=True) is True


def test_corroborated_never_blocks_regardless_of_incumbent():
    # Pooling succeeded (fresh merged object) → corroborated; promote normally
    # whether the cell was empty or occupied.
    first = _result([200.0, 200.0], "COMBAT", 0)
    confirm = _result([190.0, 190.0], "COMBAT", 0)
    pooled = _pool_confirmation(first, confirm)
    assert pooled is not first
    assert _confirm_uncorroborated(first, pooled, had_incumbent=False) is False
    assert _confirm_uncorroborated(first, pooled, had_incumbent=True) is False


# --- end-to-end landing ----------------------------------------------------

def test_empty_cell_filled_from_first_round_when_confirm_fails(tmp_path):
    arc = Archive(tmp_path / "archive")
    cell = ("COMBAT", 0)
    # Cell starts EMPTY. A would-be promotion's confirm could not corroborate.
    first = _result([200.0], "COMBAT", 0)
    confirm = EvalResult(ok=False, error="all seeds failed")
    pooled = _pool_confirmation(first, confirm)
    blocked = _confirm_uncorroborated(first, pooled, had_incumbent=False)
    assert blocked is False

    g = _genome("g_00002", cell, list(pooled.per_seed_fitness))
    r = _archive_genome(arc, g, promote=not blocked)
    assert r is not None and r.status == "filled"
    elite = arc.get_elite(cell)
    assert elite is not None and elite.id == "g_00002", (
        "an empty cell must be filled from the first round even if confirm failed"
    )


def test_occupied_cell_still_protected_when_confirm_fails(tmp_path):
    arc = Archive(tmp_path / "archive")
    cell = ("COMBAT", 0)
    # A steady incumbent already holds the cell.
    incumbent = _genome("g_00001", cell, [40.0, 40.0, 40.0])
    assert arc.add(incumbent).status == "filled"

    first = _result([200.0], "COMBAT", 0)  # lucky single round
    confirm = EvalResult(ok=False, error="all seeds failed")
    pooled = _pool_confirmation(first, confirm)
    blocked = _confirm_uncorroborated(first, pooled, had_incumbent=True)
    assert blocked is True

    g = _genome("g_00002", cell, list(pooled.per_seed_fitness))
    r = _archive_genome(arc, g, promote=not blocked)
    assert r is None, "an un-corroborated lucky run must not displace the incumbent"
    elite = arc.get_elite(cell)
    assert elite is not None and elite.id == "g_00001"
    assert arc.get("g_00002") is not None  # still persisted for lineage
