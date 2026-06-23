"""``--confirm-promotions`` must NOT spend a confirm re-eval on an EMPTY cell.

The feature exists solely to kill the optimizer's curse: a lucky single run
DISPLACING a steadier incumbent. Against an empty cell there is no incumbent to
displace, so there is no curse — and ``_confirm_uncorroborated`` already fills
the cell from the first round regardless of whether the confirm round
corroborates. So firing the confirm eval there cannot change the grid decision;
it only burns a full eval window on (nearly) every cycle while the grid is
sparse, violating the documented "fires on ~the minority of cycles" contract.

These tests pin the pure gate predicate ``_should_confirm_promotion`` across all
(incumbent?, candidate-better?) combinations. They do NOT touch the kernel
promotion rule, the held-out semantics, or the min(fitness, reliability)
selection signal.
"""
from __future__ import annotations

from foundry.orchestrator import _should_confirm_promotion


def test_empty_cell_does_not_confirm():
    # No incumbent (inc_rel is None) → nothing to displace → don't spend an eval,
    # even though the candidate's bound is high (it would "win" an empty cell
    # trivially; arc.add fills it from the first round anyway).
    assert _should_confirm_promotion(None, 200.0) is False
    assert _should_confirm_promotion(None, 0.0) is False
    assert _should_confirm_promotion(None, -5.0) is False


def test_incumbent_worse_candidate_confirms():
    # An incumbent exists AND the candidate's reliability bound beats it → this
    # is exactly the curse case (a would-be displacement): spend the confirm.
    assert _should_confirm_promotion(40.0, 200.0) is True
    assert _should_confirm_promotion(40.0, 40.0001) is True


def test_incumbent_better_or_equal_candidate_does_not_confirm():
    # The candidate would NOT displace the incumbent (arc.add would reject it on
    # the strict > rule the kernel uses), so there is no promotion to confirm.
    assert _should_confirm_promotion(200.0, 40.0) is False
    assert _should_confirm_promotion(40.0, 40.0) is False  # tie: kernel uses >


def test_gate_matches_kernel_strict_greater_promotion_rule():
    # The gate must agree with the kernel's promotion comparison (archive.add:
    # ``g.reliability > prev_rel``) on the displacement boundary, so confirm
    # fires for exactly the genomes that would actually win an OCCUPIED cell.
    inc = 100.0
    assert _should_confirm_promotion(inc, inc + 1e-9) is True   # would promote
    assert _should_confirm_promotion(inc, inc) is False         # would reject
    assert _should_confirm_promotion(inc, inc - 1e-9) is False  # would reject


def test_old_buggy_gate_would_have_fired_on_empty_cell():
    # Regression guard documenting the prior behaviour: the old gate
    # ``inc_rel is None or cand_rel > inc_rel`` fired on EVERY empty-cell landing.
    # The new predicate suppresses that wasted eval while preserving the
    # incumbent-displacement case.
    def _old_gate(inc_rel, cand_rel):
        return inc_rel is None or cand_rel > inc_rel

    # empty cell: old fires, new does not (the fix)
    assert _old_gate(None, 200.0) is True
    assert _should_confirm_promotion(None, 200.0) is False
    # occupied + would-displace: both fire (behaviour preserved)
    assert _old_gate(40.0, 200.0) is True
    assert _should_confirm_promotion(40.0, 200.0) is True
    # occupied + would-not-displace: both skip (behaviour preserved)
    assert _old_gate(200.0, 40.0) is False
    assert _should_confirm_promotion(200.0, 40.0) is False
