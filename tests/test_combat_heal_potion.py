"""Tests for the emergency heal-potion quaff in the combat loop.

A heal potion runs on a server timer independent of the bandage timer, so the
combat loop can drink one for an *instant* HP top-up while a ~8s bandage is
still applying. ``_maybe_quaff_heal_potion`` fires only when HP is critically
low, respects the potion's own ~10s cooldown (independent of the bandage
cadence), and is a clean no-op when no potion is in the pack. A potion is drunk
by a plain double-click — no target cursor.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.combat_loop as cl
from anima.client.packets import build_double_click
from anima.procedures.combat_loop import (
    POTION_COOLDOWN_S,
    POTION_HP_PCT,
    _maybe_quaff_heal_potion,
)


def _ctx(hp_pct=100.0, hits_max=100):
    ss = SimpleNamespace(hits_max=hits_max, hp_percent=hp_pct, serial=0x1)
    sent: list[bytes] = []

    async def _send(pkt):
        sent.append(pkt)

    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss),
        conn=SimpleNamespace(send_packet=_send),
        blackboard={},
        _sent=sent,
    )


def _stock_potions(monkeypatch, serial=0xBEEF):
    monkeypatch.setattr(
        cl, "find_in_backpack",
        lambda ctx, g: [SimpleNamespace(serial=serial)],
    )


class TestMaybeQuaffHealPotion:
    @pytest.mark.asyncio
    async def test_critical_hp_quaffs_via_double_click(self, monkeypatch):
        """Below POTION_HP_PCT with a potion in pack → drink it (double-click,
        no target cursor) and arm the cooldown."""
        _stock_potions(monkeypatch, serial=0xBEEF)
        monkeypatch.setattr(cl.time, "monotonic", lambda: 5000.0)

        ctx = _ctx(hp_pct=POTION_HP_PCT - 5.0)
        quaffed = await _maybe_quaff_heal_potion(ctx)

        assert quaffed is True
        # A plain double-click of the potion serial — NOT a targeted use.
        assert ctx._sent == [build_double_click(0xBEEF)]
        assert ctx.blackboard["_potion_last_ts"] == 5000.0

    @pytest.mark.asyncio
    async def test_healthy_hp_does_not_quaff(self, monkeypatch):
        """At/above POTION_HP_PCT a potion must be conserved."""
        _stock_potions(monkeypatch)
        monkeypatch.setattr(cl.time, "monotonic", lambda: 5000.0)

        ctx = _ctx(hp_pct=POTION_HP_PCT + 1.0)
        assert await _maybe_quaff_heal_potion(ctx) is False
        assert ctx._sent == []
        assert "_potion_last_ts" not in ctx.blackboard

    @pytest.mark.asyncio
    async def test_no_potion_in_pack_is_clean_noop(self, monkeypatch):
        """Availability gate: empty pack → no packet, no phantom cooldown."""
        monkeypatch.setattr(cl, "find_in_backpack", lambda ctx, g: [])
        monkeypatch.setattr(cl.time, "monotonic", lambda: 5000.0)

        ctx = _ctx(hp_pct=10.0)
        assert await _maybe_quaff_heal_potion(ctx) is False
        assert ctx._sent == []
        assert "_potion_last_ts" not in ctx.blackboard

    @pytest.mark.asyncio
    async def test_cooldown_blocks_a_second_quaff(self, monkeypatch):
        """Inside POTION_COOLDOWN_S a second drink must be suppressed even at
        critical HP — the server would refuse it."""
        _stock_potions(monkeypatch)
        ctx = _ctx(hp_pct=20.0)

        monkeypatch.setattr(cl.time, "monotonic", lambda: 5000.0)
        assert await _maybe_quaff_heal_potion(ctx) is True
        assert len(ctx._sent) == 1

        # Half a cooldown later, still critical → suppressed.
        monkeypatch.setattr(
            cl.time, "monotonic", lambda: 5000.0 + POTION_COOLDOWN_S / 2.0
        )
        assert await _maybe_quaff_heal_potion(ctx) is False
        assert len(ctx._sent) == 1  # no new packet

        # Past the cooldown → allowed again.
        monkeypatch.setattr(
            cl.time, "monotonic", lambda: 5000.0 + POTION_COOLDOWN_S + 0.1
        )
        assert await _maybe_quaff_heal_potion(ctx) is True
        assert len(ctx._sent) == 2

    @pytest.mark.asyncio
    async def test_unknown_hp_does_not_quaff(self, monkeypatch):
        """hits_max <= 0 (HP never queried) → don't burn a potion on a guess."""
        _stock_potions(monkeypatch)
        monkeypatch.setattr(cl.time, "monotonic", lambda: 5000.0)

        ctx = _ctx(hp_pct=0.0, hits_max=0)
        assert await _maybe_quaff_heal_potion(ctx) is False
        assert ctx._sent == []

    @pytest.mark.asyncio
    async def test_potion_and_bandage_cooldowns_are_independent(self, monkeypatch):
        """A potion drunk now must NOT block a bandage (and vice versa): the two
        heals run on separate timers, so the potion uses its own cooldown key.
        """
        _stock_potions(monkeypatch)
        monkeypatch.setattr(cl.time, "monotonic", lambda: 5000.0)

        ctx = _ctx(hp_pct=20.0)
        # Simulate a bandage already in flight this tick.
        ctx.blackboard["_bandage_last_ts"] = 5000.0

        assert await _maybe_quaff_heal_potion(ctx) is True
        # The potion uses its own key and never touches the bandage timer.
        assert ctx.blackboard["_potion_last_ts"] == 5000.0
        assert ctx.blackboard["_bandage_last_ts"] == 5000.0
