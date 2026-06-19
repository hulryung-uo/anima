"""Tests for DPS-adaptive self-bandage cadence in the combat loop.

A self-bandage takes ~8s to resolve. Against a single mob, starting the heal at
85%% HP is fine. Against multiple hard-hitting mobs HP can plummet past the 35%%
retreat floor in a couple of ticks, so the fixed 85%% trigger lands a heal far
too late. ``_bandage_trigger_pct`` watches the HP trajectory across ticks and
raises the trigger toward ~98%% when HP is falling steeply, so a heal is already
in flight before HP reaches dangerous levels.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.combat_loop as cl
from anima.procedures.combat_loop import (
    BANDAGE_HP_HEAVY_DPS_PCT,
    BANDAGE_HP_PCT,
    HEAVY_DPS_PCT_PER_S,
    _bandage_trigger_pct,
    _maybe_bandage,
)


def _ctx(hp_pct=100.0, hits_max=100):
    ss = SimpleNamespace(hits_max=hits_max, hp_percent=hp_pct, serial=0x1)
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss),
        blackboard={},
    )


class TestBandageTriggerPct:
    def test_first_observation_is_baseline(self):
        # No prior HP sample → no trajectory → conservative baseline trigger.
        ctx = _ctx(hp_pct=100.0)
        assert _bandage_trigger_pct(ctx, 100.0, now=1000.0) == BANDAGE_HP_PCT

    def test_steep_drop_raises_trigger(self):
        ctx = _ctx()
        _bandage_trigger_pct(ctx, 100.0, now=1000.0)          # seed history
        # Lost 20%% in 1s == 20 pct/s, well above the heavy-DPS threshold.
        trig = _bandage_trigger_pct(ctx, 80.0, now=1001.0)
        assert trig == BANDAGE_HP_HEAVY_DPS_PCT
        assert trig > BANDAGE_HP_PCT

    def test_gentle_drop_stays_baseline(self):
        ctx = _ctx()
        _bandage_trigger_pct(ctx, 100.0, now=1000.0)
        # Lost only 2%% in 1s — below the heavy-DPS threshold.
        assert _bandage_trigger_pct(ctx, 98.0, now=1001.0) == BANDAGE_HP_PCT

    def test_healing_never_inflates_trigger(self):
        # HP rising (a heal landed) must not be read as "heavy DPS".
        ctx = _ctx()
        _bandage_trigger_pct(ctx, 60.0, now=1000.0)
        assert _bandage_trigger_pct(ctx, 90.0, now=1001.0) == BANDAGE_HP_PCT

    def test_threshold_exactly_at_boundary_counts_as_heavy(self):
        ctx = _ctx()
        _bandage_trigger_pct(ctx, 100.0, now=1000.0)
        # Exactly HEAVY_DPS_PCT_PER_S over 1s == boundary, inclusive.
        trig = _bandage_trigger_pct(ctx, 100.0 - HEAVY_DPS_PCT_PER_S, now=1001.0)
        assert trig == BANDAGE_HP_HEAVY_DPS_PCT

    def test_zero_dt_falls_back_to_baseline(self):
        # Two samples at the same timestamp must not divide by zero.
        ctx = _ctx()
        _bandage_trigger_pct(ctx, 100.0, now=1000.0)
        assert _bandage_trigger_pct(ctx, 50.0, now=1000.0) == BANDAGE_HP_PCT


class TestMaybeBandageIntegration:
    @pytest.mark.asyncio
    async def test_heavy_dps_bandages_at_near_full_hp(self, monkeypatch):
        """At 90%% HP a steady fight would NOT bandage (90 >= 85), but a steep
        drop raises the trigger to ~98 so the heal fires pre-emptively."""
        used_calls = []

        monkeypatch.setattr(
            cl, "find_in_backpack",
            lambda ctx, g: [SimpleNamespace(serial=0xBA)],
        )

        async def fake_use(ctx, obj, tgt, timeout=2.0):
            used_calls.append((obj, tgt))
            return SimpleNamespace(success=True, message="ok")

        monkeypatch.setattr(cl, "use_on_object", fake_use)

        ctx = _ctx(hp_pct=100.0)
        # Tick 1 at full HP: seeds trajectory, no bandage (100 >= trigger).
        monkeypatch.setattr(cl.time, "monotonic", lambda: 1000.0)
        await _maybe_bandage(ctx)
        assert used_calls == []

        # Tick 2: dropped 100 -> 90 in 1s (10 pct/s >= heavy). Trigger becomes
        # ~98, and 90 < 98 → bandage fires even though 90 >= BANDAGE_HP_PCT.
        ctx.perception.self_state.hp_percent = 90.0
        monkeypatch.setattr(cl.time, "monotonic", lambda: 1001.0)
        await _maybe_bandage(ctx)
        assert used_calls == [(0xBA, 0x1)]
        assert ctx.blackboard["_bandage_last_ts"] == 1001.0

    @pytest.mark.asyncio
    async def test_gentle_fight_does_not_bandage_at_90pct(self, monkeypatch):
        """Sanity: without a steep drop, 90%% HP must NOT trigger a bandage —
        the baseline 85%% behavior is preserved."""
        used = AsyncMock()
        monkeypatch.setattr(
            cl, "find_in_backpack",
            lambda ctx, g: [SimpleNamespace(serial=0xBA)],
        )
        monkeypatch.setattr(cl, "use_on_object", used)

        ctx = _ctx(hp_pct=92.0)
        monkeypatch.setattr(cl.time, "monotonic", lambda: 1000.0)
        await _maybe_bandage(ctx)            # seed
        ctx.perception.self_state.hp_percent = 90.0   # lost 2%% in 1s
        monkeypatch.setattr(cl.time, "monotonic", lambda: 1001.0)
        await _maybe_bandage(ctx)
        used.assert_not_called()
