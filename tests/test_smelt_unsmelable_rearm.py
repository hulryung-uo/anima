"""A colored-ore hue blacklisted as "unsmelable" (not enough metal at the
current Mining skill) must be RE-ARMED once Mining rises far enough that the
skill-too-low verdict can flip. The blacklist is session-lifetime, but the
verdict is a property of the skill at blacklist time — Mining climbs all
session, so a never-resetting latch discards ore that became smeltable.

This pins the re-arm helper: the hue stays blacklisted while skill is flat,
and is lifted (and retried) once skill clears the recorded mark by
SMELT_REARM_SKILL_DELTA tenths.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from anima.procedures.smelt_ore import (
    SMELT_REARM_SKILL_DELTA,
    _rearm_unsmelable_on_skill_gain,
)
from anima.perception.self_state import SkillInfo
from anima.skills.crafting.smelt import MINING_SKILL_ID


def _make_ctx(mining_tenths: float) -> MagicMock:
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.skills = {MINING_SKILL_ID: SkillInfo(id=MINING_SKILL_ID, value=mining_tenths)}
    ctx.blackboard = {}
    return ctx


def _blacklist(ctx, hue: int, at_skill: float) -> None:
    ctx.blackboard.setdefault("_unsmelable_ore_hues", set()).add(hue)
    ctx.blackboard.setdefault("_unsmelable_ore_hue_skill", {})[hue] = at_skill


def test_hue_stays_blacklisted_below_skill_threshold():
    # Blacklisted at 300 tenths (30.0). Now at 300 + (delta - 1) — not enough.
    ctx = _make_ctx(300.0 + SMELT_REARM_SKILL_DELTA - 1.0)
    _blacklist(ctx, 0x0501, at_skill=300.0)

    _rearm_unsmelable_on_skill_gain(ctx)

    assert 0x0501 in ctx.blackboard["_unsmelable_ore_hues"], (
        "hue must remain blacklisted until Mining clears the re-arm delta"
    )
    assert ctx.blackboard["_unsmelable_ore_hue_skill"].get(0x0501) == 300.0


def test_hue_rearmed_once_mining_rises_past_threshold():
    # Same hue, blacklisted at 300; Mining has now climbed exactly delta past it.
    ctx = _make_ctx(300.0 + SMELT_REARM_SKILL_DELTA)
    _blacklist(ctx, 0x0501, at_skill=300.0)

    _rearm_unsmelable_on_skill_gain(ctx)

    assert 0x0501 not in ctx.blackboard["_unsmelable_ore_hues"], (
        "a hue blacklisted at low skill must be retried once Mining rises — "
        "the blacklist is a skill-relative latch, not permanent"
    )
    # The skill record for the re-armed hue is dropped so it doesn't leak.
    assert 0x0501 not in ctx.blackboard["_unsmelable_ore_hue_skill"]


def test_rearm_is_per_hue():
    # Two hues blacklisted at different skills; only the one whose recorded
    # skill is far enough below current is lifted.
    ctx = _make_ctx(400.0)
    _blacklist(ctx, 0x0501, at_skill=400.0 - SMELT_REARM_SKILL_DELTA)  # eligible
    _blacklist(ctx, 0x0502, at_skill=400.0 - 5.0)                      # too recent

    _rearm_unsmelable_on_skill_gain(ctx)

    bl = ctx.blackboard["_unsmelable_ore_hues"]
    assert 0x0501 not in bl, "the lower-skill hue should be re-armed"
    assert 0x0502 in bl, "the recently-blacklisted hue must stay blacklisted"


def test_unknown_mining_skill_does_not_rearm():
    # Mining not streamed yet (value 0.0 placeholder): must not clear anything.
    ctx = _make_ctx(0.0)
    _blacklist(ctx, 0x0501, at_skill=0.0)

    _rearm_unsmelable_on_skill_gain(ctx)

    assert 0x0501 in ctx.blackboard["_unsmelable_ore_hues"], (
        "a placeholder (unknown) Mining reading must not re-arm the blacklist"
    )


def test_noop_when_blacklist_empty():
    ctx = _make_ctx(700.0)
    # No exception, no keys created.
    _rearm_unsmelable_on_skill_gain(ctx)
    assert "_unsmelable_ore_hues" not in ctx.blackboard
