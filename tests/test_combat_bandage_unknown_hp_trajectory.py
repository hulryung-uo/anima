"""Regression: an unknown-HP tick must not poison the DPS-adaptive bandage trajectory.

``SelfState.hp_percent`` returns a 100.0 *placeholder* while ``hits_max == 0``
(the agent's own 0x11/0x16 status has not streamed in yet — common on the first
tick of a fight). ``_maybe_bandage`` used to feed that placeholder to
``_bandage_trigger_pct`` *before* the ``hits_max <= 0`` guard, seeding the
cross-tick HP trajectory with a phantom 100%. When real HP then arrived low, the
estimator saw a huge "drop" that never happened and latched the heavy-DPS
trigger (~98%) off noise. The guard now runs first, so an unknown-HP tick is a
true no-op on the trajectory and the first *known* reading is the clean baseline.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.combat_loop as cl
from anima.procedures.combat_loop import (
    BANDAGE_HP_PCT,
    _maybe_bandage,
)


def _ctx(hp_pct=100.0, hits_max=100):
    ss = SimpleNamespace(
        hits_max=hits_max, hp_percent=hp_pct, serial=0x1, is_poisoned=False
    )
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss),
        blackboard={},
    )


@pytest.fixture(autouse=True)
def _mock_clocks(monkeypatch):
    # Mock both asyncio.sleep and time.monotonic so the test is deterministic
    # and never actually sleeps (per the harness contract).
    monkeypatch.setattr(cl.asyncio, "sleep", AsyncMock())
    holder = {"t": 1000.0}
    monkeypatch.setattr(cl.time, "monotonic", lambda: holder["t"])
    return holder


class TestUnknownHpTrajectory:
    @pytest.mark.asyncio
    async def test_unknown_hp_tick_does_not_seed_trajectory(self, _mock_clocks):
        """A tick with hits_max == 0 (placeholder hp_percent=100) must NOT write
        the DPS-baseline blackboard keys — it is not a real reading."""
        ctx = _ctx(hp_pct=100.0, hits_max=0)
        await _maybe_bandage(ctx)
        # No trajectory state was recorded off the placeholder.
        assert "_bandage_hp_last" not in ctx.blackboard
        assert "_bandage_hp_ts" not in ctx.blackboard

    @pytest.mark.asyncio
    async def test_unknown_then_known_drop_uses_real_baseline(self, _mock_clocks):
        """The regression: unknown-HP tick (placeholder 100), then the first real
        reading at 60%. The trajectory baseline must be that 60% reading — NOT
        the phantom 100 — so a steep 'drop' is not fabricated on the FOLLOWING
        known tick, and no bandage fires off noise while HP is healthy."""
        used = AsyncMock(return_value=SimpleNamespace(success=True, message="ok"))
        monkeypatch_use = used
        # Stock a bandage and capture any apply.
        ctx = _ctx(hp_pct=100.0, hits_max=0)
        # Patch via the module so find_in_backpack / use_on_object are mocked.
        import anima.procedures.combat_loop as mod
        mod.find_in_backpack = lambda ctx, g: [SimpleNamespace(serial=0xBA)]
        mod.use_on_object = monkeypatch_use

        # Tick 1 — HP unknown (hits_max == 0). No-op on trajectory, no bandage.
        _mock_clocks["t"] = 1000.0
        await _maybe_bandage(ctx)
        assert used.await_count == 0
        assert "_bandage_hp_last" not in ctx.blackboard

        # Tick 2 — first REAL reading: hits_max known, HP at 60%. This is the
        # clean baseline; 60 < BANDAGE_HP_PCT so a bandage is warranted here, but
        # the trajectory must record 60 (not be compared against a phantom 100).
        ctx.perception.self_state.hits_max = 100
        ctx.perception.self_state.hp_percent = 60.0
        _mock_clocks["t"] = 1001.0
        await _maybe_bandage(ctx)
        assert ctx.blackboard["_bandage_hp_last"] == 60.0
        assert ctx.blackboard["_bandage_hp_ts"] == 1001.0
        used.reset_mock()
        # Re-arm: clear the just-set reapply cooldown so the next eligible tick
        # is gated only by the HP trigger, not the 8.5s spacing.
        ctx.blackboard["_bandage_last_ts"] = 0.0

        # Tick 3 — HP RECOVERS to 95% over 1s (a heal landed). Drop is negative,
        # so the trigger stays at the baseline BANDAGE_HP_PCT (85) and 95 >= 85
        # → NO bandage. Had tick-1's phantom 100 survived, the tick-2 comparison
        # would already have mis-fired; this asserts the real baseline holds.
        ctx.perception.self_state.hp_percent = 95.0
        _mock_clocks["t"] = 1002.0
        await _maybe_bandage(ctx)
        assert used.await_count == 0
        assert ctx.blackboard["_bandage_hp_last"] == 95.0

    @pytest.mark.asyncio
    async def test_known_low_then_known_high_no_phantom_heavy_dps(self, _mock_clocks):
        """Pure-known control: two real readings where HP RISES must keep the
        baseline trigger (no heavy-DPS latch) — guards against any regression in
        the reordered guard accidentally inverting the sign of the trajectory."""
        from anima.procedures.combat_loop import _bandage_trigger_pct

        ctx = _ctx()
        assert _bandage_trigger_pct(ctx, 50.0, now=1000.0) == BANDAGE_HP_PCT
        # HP rose to 90 over 1s → negative drop → baseline trigger preserved.
        assert _bandage_trigger_pct(ctx, 90.0, now=1001.0) == BANDAGE_HP_PCT
