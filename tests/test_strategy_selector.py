"""Tests for the strategy selector."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import patch

import pytest

from anima.planner.strategy import (
    ALL_STRATEGIES,
    STRATEGY_GRIND_MINING,
    STRATEGY_SELL_INVENTORY,
    STRATEGY_EXCLUSIONS,
    StrategyDecision,
    StrategySelector,
)


class TestStrategySelectorBasics:
    def test_default_strategy(self):
        s = StrategySelector()
        assert s.current.name == STRATEGY_GRIND_MINING

    def test_min_interval_enforced(self):
        s = StrategySelector(interval_s=5)
        assert s.interval_s >= 60.0

    def test_should_refresh_false_initially(self):
        s = StrategySelector(interval_s=60)
        # Not refreshed yet, last_refresh=0.0 — should_refresh returns True
        # actually because (now - 0) >> 60. So initial state IS refresh-eligible.
        assert s.should_refresh() is True

    def test_should_refresh_false_right_after_refresh(self):
        s = StrategySelector(interval_s=60)
        # Cadence runs on the monotonic clock now (immune to wall-clock steps).
        s._last_refresh = time.monotonic()
        assert s.should_refresh() is False

    def test_should_refresh_uses_monotonic_clock(self):
        """The refresh gate must read time.monotonic(), not time.time().

        Regression: a wall-clock gate over-waits when the host clock steps
        backward (NTP correction / VM resume). With a monotonic gate the
        elapsed interval is measured against a clock that never jumps, so a
        wall-clock step does not change refresh eligibility.
        """
        s = StrategySelector(interval_s=60)
        # Last refresh happened "100s ago" on the monotonic clock.
        with patch("anima.planner.strategy.time.monotonic", return_value=1_000.0):
            s._last_refresh = s_last = None  # noqa: F841 — clarity only
            s._last_refresh = 900.0  # 100s before the patched 'now'
            # time.time() is left at its real (huge) value; if the gate read
            # wall time it would always be eligible regardless of the interval.
            assert s.should_refresh() is True  # 1000 - 900 = 100 >= 60

        with patch("anima.planner.strategy.time.monotonic", return_value=1_000.0):
            s._last_refresh = 970.0  # only 30s before 'now' → not yet eligible
            assert s.should_refresh() is False  # 1000 - 970 = 30 < 60

    def test_should_refresh_immune_to_backward_wallclock_step(self):
        """A backward wall-clock step must NOT freeze refreshes.

        Stamp a refresh, then simulate the wall clock jumping far into the
        past while the monotonic clock keeps advancing past the interval.
        The gate must re-open on monotonic progress alone.
        """
        s = StrategySelector(interval_s=60)
        with patch("anima.planner.strategy.time.monotonic", return_value=5_000.0):
            s._last_refresh = time.monotonic()  # stamp at 5000
        # Monotonic advanced 120s (> interval) even though wall time may have
        # jumped backward; the gate must be eligible again.
        with patch("anima.planner.strategy.time.monotonic", return_value=5_120.0):
            assert s.should_refresh() is True


class TestStrategyExclusions:
    def test_grind_mining_allows_full_loop(self):
        s = StrategySelector()
        s._current = StrategyDecision(name=STRATEGY_GRIND_MINING, reasoning="x")
        s._active = True  # simulate post-first-LLM-refresh state
        # Full mining loop: mine → smelt → craft → sell → mine.
        # None of these should be excluded.
        assert s.is_excluded("mine_ore") is False
        assert s.is_excluded("smelt_ore") is False
        assert s.is_excluded("craft_blacksmith") is False
        assert s.is_excluded("sell_to_vendor") is False

    def test_sell_inventory_excludes_mining_only(self):
        """sell_inventory blocks further gathering (mine_ore) but keeps
        smelt_ore and craft_blacksmith — they're prerequisites for
        actually disposing of backpack contents."""
        s = StrategySelector()
        s._current = StrategyDecision(name=STRATEGY_SELL_INVENTORY, reasoning="x")
        s._active = True
        assert s.is_excluded("mine_ore") is True
        assert s.is_excluded("smelt_ore") is False
        assert s.is_excluded("craft_blacksmith") is False
        assert s.is_excluded("sell_to_vendor") is False

    def test_unknown_procedure_not_excluded(self):
        s = StrategySelector()
        assert s.is_excluded("never_heard_of_it") is False


async def _drain_refresh(s: StrategySelector) -> None:
    """Await the background refresh task spawned by maybe_refresh."""
    if s._refresh_task is not None:
        await s._refresh_task


class TestMaybeRefreshLLM:
    @pytest.mark.asyncio
    async def test_no_llm_stays_on_default(self):
        s = StrategySelector()
        ctx = MagicMock()
        ctx.llm = None
        spawned = await s.maybe_refresh(ctx)
        await _drain_refresh(s)
        # Spawn happened, but with no LLM the task is a no-op
        assert spawned is True
        assert s.current.name == STRATEGY_GRIND_MINING

    @pytest.mark.asyncio
    async def test_llm_valid_response_updates_strategy(self):
        s = StrategySelector()
        s._last_refresh = 0.0  # eligible for refresh

        ctx = MagicMock()
        fake_llm = MagicMock()
        fake_llm.chat = AsyncMock(return_value=MagicMock(
            text="STRATEGY: sell_inventory\nREASONING: inventory too full"
        ))
        ctx.llm = fake_llm
        ctx.perception.self_state.gold = 100
        ctx.perception.self_state.weight = 300
        ctx.perception.self_state.weight_max = 400
        ctx.perception.self_state.equipment = {0x15: 0x101}
        # Provide one sellable iron ingot so the sanity check accepts
        # sell_inventory (otherwise it falls back to grind_mining).
        ingot = MagicMock(container=0x101, graphic=0x1BF2, amount=5, hue=0)
        ctx.perception.world.items = {0x200: ingot}

        spawned = await s.maybe_refresh(ctx)
        await _drain_refresh(s)
        assert spawned is True
        assert s.current.name == STRATEGY_SELL_INVENTORY
        assert "inventory too full" in s.current.reasoning

    @pytest.mark.asyncio
    async def test_llm_sell_inventory_rejected_when_nothing_to_sell(self):
        """Sanity check overrides LLM when no sellable items exist."""
        s = StrategySelector()
        s._last_refresh = 0.0

        ctx = MagicMock()
        fake_llm = MagicMock()
        fake_llm.chat = AsyncMock(return_value=MagicMock(
            text="STRATEGY: sell_inventory\nREASONING: free up weight"
        ))
        ctx.llm = fake_llm
        ctx.perception.self_state.gold = 100
        ctx.perception.self_state.weight = 50
        ctx.perception.self_state.weight_max = 400
        ctx.perception.self_state.equipment = {0x15: 0x101}
        # Empty backpack — nothing sellable
        ctx.perception.world.items = {}

        spawned = await s.maybe_refresh(ctx)
        await _drain_refresh(s)
        assert spawned is True
        assert s.current.name == STRATEGY_GRIND_MINING
        assert "auto-fallback" in s.current.reasoning

    @pytest.mark.asyncio
    async def test_llm_unknown_response_rejected(self):
        s = StrategySelector()
        s._last_refresh = 0.0

        ctx = MagicMock()
        fake_llm = MagicMock()
        fake_llm.chat = AsyncMock(return_value=MagicMock(
            text="STRATEGY: bogus_mode\nREASONING: ..."
        ))
        ctx.llm = fake_llm
        ctx.perception.self_state.gold = 0
        ctx.perception.self_state.weight = 0
        ctx.perception.self_state.weight_max = 400
        ctx.perception.self_state.equipment = {0x15: 0x101}
        ctx.perception.world.items = {}

        await s.maybe_refresh(ctx)
        await _drain_refresh(s)
        # Unknown response → stays on default
        assert s.current.name == STRATEGY_GRIND_MINING

    @pytest.mark.asyncio
    async def test_llm_exception_does_not_crash(self):
        s = StrategySelector()
        s._last_refresh = 0.0

        ctx = MagicMock()
        fake_llm = MagicMock()
        fake_llm.chat = AsyncMock(side_effect=RuntimeError("network error"))
        ctx.llm = fake_llm
        ctx.perception.self_state.gold = 0
        ctx.perception.self_state.weight = 0
        ctx.perception.self_state.weight_max = 400
        ctx.perception.self_state.equipment = {0x15: 0x101}
        ctx.perception.world.items = {}

        spawned = await s.maybe_refresh(ctx)
        await _drain_refresh(s)
        # Spawn succeeded; task swallowed the LLM exception
        assert spawned is True
        assert s.current.name == STRATEGY_GRIND_MINING

    @pytest.mark.asyncio
    async def test_maybe_refresh_does_not_block_on_slow_llm(self):
        """Regression: maybe_refresh must return immediately even when the LLM
        would take minutes to respond. Previously the planner awaited the LLM
        inline, which stalled procedure selection long enough for the server
        connection-silence watchdog to kill the agent on every restart.
        """
        s = StrategySelector()
        s._last_refresh = 0.0

        slow_done = asyncio.Event()

        async def slow_chat(_messages):
            await slow_done.wait()  # never completes during this test
            return MagicMock(text="STRATEGY: grind_mining\nREASONING: x")

        ctx = MagicMock()
        fake_llm = MagicMock()
        fake_llm.chat = slow_chat
        ctx.llm = fake_llm
        ctx.perception.self_state.gold = 0
        ctx.perception.self_state.weight = 0
        ctx.perception.self_state.weight_max = 400
        ctx.perception.self_state.equipment = {0x15: 0x101}
        ctx.perception.world.items = {}

        # Should return promptly without waiting for slow_chat to finish.
        spawned = await asyncio.wait_for(s.maybe_refresh(ctx), timeout=0.5)
        assert spawned is True
        assert s._refresh_task is not None
        assert not s._refresh_task.done()

        # A second call while the task is in flight should be a no-op
        # (single-flight) — and also return promptly.
        spawned_again = await asyncio.wait_for(s.maybe_refresh(ctx), timeout=0.5)
        assert spawned_again is False

        # Cleanup so the task does not leak past the test.
        s._refresh_task.cancel()
        try:
            await s._refresh_task
        except (asyncio.CancelledError, BaseException):
            pass


class TestCloseReapsRefreshTask:
    @pytest.mark.asyncio
    async def test_close_cancels_in_flight_refresh(self):
        """Regression: close() must cancel+await the detached refresh task.

        maybe_refresh spawns _do_refresh with a bare asyncio.create_task that
        is not a TaskGroup child, so without an explicit reap on shutdown it
        outlives the session (suspended in a slow LLM call) holding ctx/llm.
        """
        s = StrategySelector()
        s._last_refresh = 0.0

        slow_done = asyncio.Event()

        async def slow_chat(_messages):
            await slow_done.wait()  # never completes during this test
            return MagicMock(text="STRATEGY: grind_mining\nREASONING: x")

        ctx = MagicMock()
        fake_llm = MagicMock()
        fake_llm.chat = slow_chat
        ctx.llm = fake_llm
        ctx.perception.self_state.gold = 0
        ctx.perception.self_state.weight = 0
        ctx.perception.self_state.weight_max = 400
        ctx.perception.self_state.equipment = {0x15: 0x101}
        ctx.perception.world.items = {}
        # No active expedition cycle so _do_refresh reaches the LLM await.
        ctx.blackboard = {}

        spawned = await asyncio.wait_for(s.maybe_refresh(ctx), timeout=0.5)
        assert spawned is True
        task = s._refresh_task
        assert task is not None
        assert not task.done()

        # Shutdown must cancel and reap the in-flight task — and clear the ref.
        await asyncio.wait_for(s.close(), timeout=0.5)
        assert task.cancelled()
        assert s._refresh_task is None

    @pytest.mark.asyncio
    async def test_close_is_idempotent_with_no_task(self):
        """close() is a safe no-op when no refresh has ever been spawned."""
        s = StrategySelector()
        assert s._refresh_task is None
        await s.close()  # must not raise
        await s.close()  # idempotent
        assert s._refresh_task is None
