"""``observe._smells`` must not flag a structurally-short eval as "NO OUTPUT".

The "NO OUTPUT: nothing gathered/crafted and no skill gained — net
unproductive" hint shares its zero-skill condition with the sibling
"NOT PRACTICING ITS TRADE" hint, which is gated on ``duration_h > 0.05`` (≈180s)
precisely because a short window can't be judged on productivity. "NO OUTPUT"
was missing that guard, so a few-second successful eval (window cut short / early
disconnect) — where empty ``items_into_pack`` and ~0 skill gain are EXPECTED, not
a defect — still emitted "NO OUTPUT", misdirecting the mutator's single
per-cycle attention budget at a phantom problem and contradicting the
(correctly un-emitted) "NOT PRACTICING" smell on the very same run.
"""
from foundry.kernel.eval import EvalResult
from foundry.kernel.fitness import FitnessBreakdown
from foundry.kernel.trajectory import TrajectorySummary
from foundry.observe import _smells


def _result(duration_s: float) -> EvalResult:
    # A "clean but produced nothing" eval: alive + active (no LOW LIVENESS /
    # STUCK WALKING), no deaths, zero skill gain, empty pack. The ONLY smell at
    # issue is "NO OUTPUT", which must fire only once the window is long enough
    # to judge (matching the "NOT PRACTICING" guard).
    summ = TrajectorySummary(start_ts=1000.0, end_ts=1000.0 + duration_s)
    summ.items_into_pack = []
    fit = FitnessBreakdown(
        liveness=1.0,        # > 0.3 → no LOW LIVENESS smell
        loop_penalty=0.0,    # ≤ 0.3 → no STUCK WALKING smell
        skill_gain_rate=0.0, # < 0.5 → triggers the gated smells
    )
    return EvalResult(ok=True, fitness=fit, summary=summ)


def _has_no_output(res: EvalResult) -> bool:
    return any(sm.startswith("NO OUTPUT") for sm in _smells(res))


def _has_not_practicing(res: EvalResult) -> bool:
    return any(sm.startswith("NOT PRACTICING") for sm in _smells(res))


def test_short_eval_does_not_flag_no_output():
    # 5s window → duration_h ≈ 0.0014 < 0.05: too short to judge productivity.
    res = _result(duration_s=5.0)
    assert not _has_no_output(res)
    # Consistency: its sibling zero-skill smell is (correctly) also withheld.
    assert not _has_not_practicing(res)


def test_long_eval_still_flags_no_output():
    # 600s window → duration_h ≈ 0.167 > 0.05: long enough — a genuinely
    # unproductive run must still surface "NO OUTPUT" for the mutator.
    res = _result(duration_s=600.0)
    assert _has_no_output(res)
    assert _has_not_practicing(res)


def test_no_output_guard_matches_sibling_threshold():
    # The two zero-skill smells must agree on what counts as "too short to
    # judge": across the 0.05h (180s) boundary they flip together, never one
    # without the other.
    just_under = _result(duration_s=180.0 * 3600 ** -1 * 3600 - 1.0)  # 179s
    just_over = _result(duration_s=181.0)
    assert _has_no_output(just_under) == _has_not_practicing(just_under) is False
    assert _has_no_output(just_over) == _has_not_practicing(just_over) is True


def test_long_eval_with_output_is_not_flagged():
    # Produced an item over a long window → not "NO OUTPUT" regardless of skill.
    res = _result(duration_s=600.0)
    res.summary.items_into_pack = [(0x1BF2, 5, 1300.0)]  # 5 ingots into pack
    assert not _has_no_output(res)
