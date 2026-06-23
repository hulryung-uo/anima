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


def test_rearm_also_clears_blocked_until_time_latch():
    # make_tools.execute arms BOTH latches together on a "required skill" refusal:
    # the _make_tools_gave_up boolean AND the 300s _tinkering_blocked_until clock.
    # Both planner gates (4b craft-for-tools, 5e Tinkering-training) AND-gate on
    # `not _make_tools_gave_up` *and* `time.time() >= _tinkering_blocked_until`.
    # When skill climbs enough to lift the boolean, the time-latch must clear too —
    # otherwise the planner keeps refusing make_tools for the rest of the 300s
    # window, re-blocking the path this re-arm exists to re-open.
    import time

    ctx = _make_ctx(200.0 + TINKERING_REARM_SKILL_DELTA)
    _arm(ctx, at_skill=200.0)
    # Sibling time-latch armed in the same refusal, still well in the future.
    ctx.blackboard["_tinkering_blocked_until"] = time.time() + 300.0

    _rearm_gave_up_on_skill_gain(ctx)

    assert "_make_tools_gave_up" not in ctx.blackboard
    assert "_tinkering_blocked_until" not in ctx.blackboard, (
        "the 300s block-until time-latch armed together with the give-up boolean "
        "must clear together with it — leaving it live re-blocks the planner's "
        "make_tools gate for the rest of the window on the same skill gain that "
        "just lifted the boolean verdict"
    )


def test_blocked_until_preserved_while_latch_stays_armed():
    # Below the re-arm threshold: the give-up verdict still holds, so the sibling
    # time-latch must NOT be cleared early (it still gates the planner correctly).
    import time

    ctx = _make_ctx(200.0 + TINKERING_REARM_SKILL_DELTA - 1.0)
    _arm(ctx, at_skill=200.0)
    future = time.time() + 300.0
    ctx.blackboard["_tinkering_blocked_until"] = future

    _rearm_gave_up_on_skill_gain(ctx)

    assert ctx.blackboard.get("_make_tools_gave_up") is True
    assert ctx.blackboard.get("_tinkering_blocked_until") == future, (
        "while the give-up latch stays armed the time-latch must be left intact"
    )
