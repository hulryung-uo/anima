"""cast_spell must classify a mid-cast disruption distinctly.

When an OFFENSIVE caster is hit while the spell is still in its cast delay,
ServUO (Spell.cs OnDisturb) emits cliloc 500641 "Your concentration is
disturbed, thus ruining thy spell." and never sends a target cursor. The mana
was not yet consumed, so this must NOT be confused with an out-of-mana /
out-of-reagents abort: the caller's right move is to recast, not to go
meditate or restock. Regression test for the disrupted-cast flag.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.actions import spells
from anima.actions.result import ActionResult


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.conn.send_packet = AsyncMock()
    ss = ctx.perception.self_state
    ss.mana = 50
    ss.mana_max = 50
    ss.serial = 0x0001A2B3
    ss.pending_target = None
    return ctx


@pytest.mark.asyncio
async def test_cast_spell_disrupted_sets_flag_not_no_mana():
    """A 500641 disrupt line → disrupted=True, NOT no_mana / no_reagents."""
    ctx = _ctx()

    async def no_cursor(_ctx, timeout=3.0):
        # Hurt mid-cast: the spell is ruined before any cursor is sent.
        return ActionResult(success=False, message="timeout")

    async def disrupt_journal(_ctx, signals, timeout=1.5, since=0.0):
        # The abort path must include the disrupt signal in its watch list.
        assert spells._DISRUPTED in signals
        idx = signals.index(spells._DISRUPTED)
        return ActionResult(
            success=True,
            data={
                "index": idx,
                "text": "Your concentration is disturbed, thus ruining thy spell.",
            },
        )

    with patch.object(spells, "wait_for_target", no_cursor), \
         patch.object(spells, "wait_for_journal", disrupt_journal):
        res = await spells.cast_spell(
            ctx, spells.SPELL_HEAL, target_serial=0x77,
            mana_cost=spells.GREATER_HEAL_MANA,
        )

    assert res.success is False
    assert res.disrupted is True
    # The whole point: a disruption is NOT a resource shortage.
    assert res.no_mana is False
    assert res.no_reagents is False
    assert res.fizzled is False


@pytest.mark.asyncio
async def test_cast_spell_no_cursor_no_journal_is_not_disrupted():
    """A silent no-cursor timeout (no journal line) leaves disrupted False."""
    ctx = _ctx()

    async def no_cursor(_ctx, timeout=3.0):
        return ActionResult(success=False, message="timeout")

    async def silent_journal(_ctx, signals, timeout=1.5, since=0.0):
        return ActionResult(success=False)

    with patch.object(spells, "wait_for_target", no_cursor), \
         patch.object(spells, "wait_for_journal", silent_journal):
        res = await spells.cast_spell(
            ctx, spells.SPELL_HEAL, target_serial=0x77, mana_cost=0,
        )

    assert res.success is False
    assert res.disrupted is False
    assert res.no_mana is False
    assert res.no_reagents is False


@pytest.mark.asyncio
async def test_cast_spell_no_mana_line_still_wins_over_disrupt():
    """An explicit Insufficient-mana abort is reported as no_mana, not disrupt.

    _NO_MANA is first in the watch list, so when the server reports the mana
    abort it takes precedence over the (also-watched) disrupt signal.
    """
    ctx = _ctx()

    async def no_cursor(_ctx, timeout=3.0):
        return ActionResult(success=False, message="timeout")

    async def mana_journal(_ctx, signals, timeout=1.5, since=0.0):
        idx = signals.index(spells._NO_MANA)
        return ActionResult(
            success=True,
            data={"index": idx, "text": "Insufficient mana"},
        )

    with patch.object(spells, "wait_for_target", no_cursor), \
         patch.object(spells, "wait_for_journal", mana_journal):
        res = await spells.cast_spell(
            ctx, spells.SPELL_HEAL, target_serial=0x77, mana_cost=0,
        )

    assert res.no_mana is True
    assert res.disrupted is False
