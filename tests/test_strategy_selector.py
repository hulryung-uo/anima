"""Tests for the strategy selector."""
import time
from unittest.mock import AsyncMock, MagicMock

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
        s._last_refresh = time.time()
        assert s.should_refresh() is False


class TestStrategyExclusions:
    def test_grind_mining_excludes_craft_but_allows_sell(self):
        s = StrategySelector()
        s._current = StrategyDecision(name=STRATEGY_GRIND_MINING, reasoning="x")
        s._active = True  # simulate post-first-LLM-refresh state
        assert s.is_excluded("craft_blacksmith") is True
        # sell_to_vendor is allowed: mining loop is mine→smelt→sell→mine
        assert s.is_excluded("sell_to_vendor") is False
        assert s.is_excluded("mine_ore") is False

    def test_sell_inventory_excludes_gathering(self):
        s = StrategySelector()
        s._current = StrategyDecision(name=STRATEGY_SELL_INVENTORY, reasoning="x")
        s._active = True  # simulate post-first-LLM-refresh state
        assert s.is_excluded("mine_ore") is True
        assert s.is_excluded("smelt_ore") is True
        assert s.is_excluded("sell_to_vendor") is False

    def test_unknown_procedure_not_excluded(self):
        s = StrategySelector()
        assert s.is_excluded("never_heard_of_it") is False


class TestMaybeRefreshLLM:
    @pytest.mark.asyncio
    async def test_no_llm_stays_on_default(self):
        s = StrategySelector()
        ctx = MagicMock()
        ctx.llm = None
        changed = await s.maybe_refresh(ctx)
        assert changed is False
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
        ctx.perception.world.items = {}

        changed = await s.maybe_refresh(ctx)
        assert changed is True
        assert s.current.name == STRATEGY_SELL_INVENTORY
        assert "inventory too full" in s.current.reasoning

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

        changed = await s.maybe_refresh(ctx)
        assert changed is False
        assert s.current.name == STRATEGY_GRIND_MINING
