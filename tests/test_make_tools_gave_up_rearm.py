"""The ``_make_tools_gave_up`` latch armed by a "required skill" / skill-too-low
gump refusal is a SKILL-RELATIVE verdict, not a permanent one. The planner gates
both the craft-for-tools (4b) and the Tinkering-training (5e) paths on
``not _make_tools_gave_up`` — so if the latch never clears it disables the only
procedure that raises Tinkering, making the give-up self-reinforcing forever.

These pin the re-arm helper: the latch stays armed while Tinkering is flat (or
only inches up), and is lifted once skill clears the recorded mark by
TINKERING_REARM_SKILL_DELTA tenths.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from anima.perception.self_state import SkillInfo
from anima.procedures.make_tools import (
    TINKERING_REARM_SKILL_DELTA,
    TINKERING_SKILL_ID,
    _rearm_gave_up_on_skill_gain,
)


def _make_ctx(tinker_tenths: float) -> MagicMock:
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.skills = {
        TINKERING_SKILL_ID: SkillInfo(id=TINKERING_SKILL_ID, value=tinker_tenths)
    }
    ctx.blackboard = {}
    return ctx


def _arm(ctx, at_skill: float) -> None:
    ctx.blackboard["_make_tools_gave_up"] = True
    ctx.blackboard["_make_tools_gave_up_skill"] = at_skill


def test_latch_stays_armed_below_skill_threshold():
    # Armed at 200 tenths (20.0). Now at 200 + (delta - 1) — not enough yet.
    ctx = _make_ctx(200.0 + TINKERING_REARM_SKILL_DELTA - 1.0)
    _arm(ctx, at_skill=200.0)

    _rearm_gave_up_on_skill_gain(ctx)

    assert ctx.blackboard.get("_make_tools_gave_up") is True, (
        "latch must stay armed until Tinkering clears the re-arm delta"
    )
    assert ctx.blackboard.get("_make_tools_gave_up_skill") == 200.0


def test_latch_rearmed_once_tinkering_rises_past_threshold():
    # Same latch, armed at 200; Tinkering has now climbed exactly delta past it.
    ctx = _make_ctx(200.0 + TINKERING_REARM_SKILL_DELTA)
    _arm(ctx, at_skill=200.0)

    _rearm_gave_up_on_skill_gain(ctx)

    assert "_make_tools_gave_up" not in ctx.blackboard, (
        "a give-up armed at low Tinkering must be lifted once skill rises — "
        "the latch is a skill-relative verdict, not permanent"
    )
    # The baseline record is dropped so it doesn't leak / re-trigger spuriously.
    assert "_make_tools_gave_up_skill" not in ctx.blackboard


def test_unknown_tinkering_skill_does_not_rearm():
    # Tinkering not streamed yet (0.0 placeholder): must not clear the latch.
    ctx = _make_ctx(0.0)
    _arm(ctx, at_skill=0.0)

    _rearm_gave_up_on_skill_gain(ctx)

    assert ctx.blackboard.get("_make_tools_gave_up") is True, (
        "a placeholder (unknown) Tinkering reading must not re-arm the latch"
    )


def test_latch_without_baseline_adopts_current_reading():
    # Latch present but no recorded baseline (legacy arm path): the helper
    # adopts the current reading and leaves the latch armed, so a LATER gain
    # can still re-arm it (instead of clearing immediately on a stale read).
    ctx = _make_ctx(150.0)
    ctx.blackboard["_make_tools_gave_up"] = True  # no _make_tools_gave_up_skill

    _rearm_gave_up_on_skill_gain(ctx)

    assert ctx.blackboard.get("_make_tools_gave_up") is True
    assert ctx.blackboard.get("_make_tools_gave_up_skill") == 150.0

    # A subsequent sufficient gain now lifts it.
    ctx.perception.self_state.skills[TINKERING_SKILL_ID] = SkillInfo(
        id=TINKERING_SKILL_ID, value=150.0 + TINKERING_REARM_SKILL_DELTA
    )
    _rearm_gave_up_on_skill_gain(ctx)
    assert "_make_tools_gave_up" not in ctx.blackboard


def test_noop_when_latch_not_set():
    ctx = _make_ctx(700.0)
    # No latch armed -> no exception, no keys created.
    _rearm_gave_up_on_skill_gain(ctx)
    assert "_make_tools_gave_up" not in ctx.blackboard
    assert "_make_tools_gave_up_skill" not in ctx.blackboard
