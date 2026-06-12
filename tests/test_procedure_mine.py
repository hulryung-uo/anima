"""Tests for MineOre procedure (Step 4)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.procedures.base import FailureReason, ProcedureResult
from anima.procedures.mine_ore import MineOre


def _make_ctx():
    ctx = MagicMock()
    ctx.perception.self_state.x = 2500
    ctx.perception.self_state.y = 550
    ctx.perception.self_state.z = 15
    ctx.perception.self_state.serial = 0x100
    ctx.perception.self_state.hits = 100
    ctx.perception.self_state.hits_max = 100
    ctx.perception.self_state.pending_target = None
    ctx.perception.self_state.equipment = {0x15: 0x101}  # backpack
    ctx.perception.world.items = {}
    ctx.conn.send_packet = AsyncMock()
    ctx.bus = None
    ctx.memory_db = None
    ctx.persona = MagicMock()
    ctx.persona.name = "TestMiner"
    return ctx


class TestMineOreCanStart:
    @pytest.mark.asyncio
    async def test_no_pickaxe(self):
        proc = MineOre()
        ctx = _make_ctx()
        # No items in backpack
        assert not await proc.can_start(ctx)

    @pytest.mark.asyncio
    async def test_has_pickaxe_no_tile(self):
        proc = MineOre()
        ctx = _make_ctx()
        # Add pickaxe to backpack
        pickaxe = MagicMock(container=0x101, graphic=0x0E86, amount=1, serial=0x200)
        ctx.perception.world.items = {0x200: pickaxe}
        # No map reader → no tiles
        ctx.map_reader = None
        # _find_mineable_tile checks map statics, will return None
        assert not await proc.can_start(ctx)

    @pytest.mark.asyncio
    async def test_diagnose_no_pickaxe(self):
        proc = MineOre()
        ctx = _make_ctx()
        reason = await proc.diagnose(ctx)
        assert reason is not None
        assert "pickaxe" in reason


class TestMineOreExecute:
    @pytest.mark.asyncio
    async def test_missing_pickaxe(self):
        proc = MineOre()
        ctx = _make_ctx()
        result = await proc.execute(ctx)
        assert not result.success
        assert result.reason == FailureReason.MISSING_RESOURCE

    @pytest.mark.asyncio
    async def test_interrupted_by_low_hp(self):
        proc = MineOre()
        ctx = _make_ctx()
        # Add pickaxe
        pickaxe = MagicMock(container=0x101, graphic=0x0E86, amount=1, serial=0x200)
        ctx.perception.world.items = {0x200: pickaxe}

        # Set low HP
        ctx.perception.self_state.hits = 20
        ctx.perception.self_state.hits_max = 100

        # Mock _find_mineable_tile to return a tile
        with patch("anima.procedures.mine_ore._find_mineable_tile") as mock_find:
            mock_find.return_value = (2501, 540, 15, 220, False)
            result = await proc.execute(ctx)

        assert not result.success
        assert result.reason == FailureReason.INTERRUPTED

    @pytest.mark.asyncio
    async def test_no_tile(self):
        proc = MineOre()
        ctx = _make_ctx()
        pickaxe = MagicMock(container=0x101, graphic=0x0E86, amount=1, serial=0x200)
        ctx.perception.world.items = {0x200: pickaxe}
        ctx.perception.self_state.hits = 100
        ctx.perception.self_state.hits_max = 100

        with patch("anima.procedures.mine_ore._find_mineable_tile") as mock_find:
            mock_find.return_value = None
            result = await proc.execute(ctx)

        assert not result.success
        assert result.reason == FailureReason.WRONG_LOCATION


class TestMineOreRun:
    @pytest.mark.asyncio
    async def test_run_wraps_execute(self):
        """run() should wrap execute() with timing and ActionLog."""
        proc = MineOre()
        ctx = _make_ctx()
        result = await proc.run(ctx)
        # Should fail gracefully (no pickaxe)
        assert not result.success
        assert result.reason == FailureReason.MISSING_RESOURCE


def _setup_mine_success_ctx(ctx):
    """Rig ctx so MineOre.execute reaches the ore_gained > 0 branch.

    Returns (before_items, after_items, flip_to_after) — callers should
    install a PropertyMock on type(ctx.perception.world).items that returns
    before_items when flip_to_after[0] is False, else after_items.
    """
    from unittest.mock import MagicMock

    pickaxe = MagicMock(container=0x101, graphic=0x0E86, amount=1, serial=0x200)
    ore_before_item = MagicMock(container=0x101, graphic=0x19B9, amount=1, serial=0x300, hue=0)
    ore_after_item = MagicMock(container=0x101, graphic=0x19B9, amount=2, serial=0x300, hue=0)

    before_items = {0x200: pickaxe, 0x300: ore_before_item}
    after_items = {0x200: pickaxe, 0x300: ore_after_item}
    return before_items, after_items


class TestMineOreExpeditionHook:
    @pytest.mark.asyncio
    async def test_successful_mine_registers_pile(self):
        """A successful mine adds a pile and transitions IDLE → MINING."""
        from unittest.mock import patch, AsyncMock, MagicMock, PropertyMock

        from anima.planner.expedition import MiningExpedition, Phase
        from anima.procedures.mine_ore import MineOre
        from anima.skills.gathering.mine import _bank_key

        proc = MineOre()
        exp = MiningExpedition()
        ctx = _make_ctx()
        ctx.blackboard = {"expedition": exp}

        before_items, after_items = _setup_mine_success_ctx(ctx)
        after_mine = [False]

        async def _use_on_target(*args, **kwargs):
            after_mine[0] = True
            return MagicMock(success=True, message="ok")

        def _items_getter(self):
            return after_items if after_mine[0] else before_items

        type(ctx.perception.world).items = PropertyMock(side_effect=lambda: _items_getter(None))

        with patch(
            "anima.procedures.mine_ore._find_mineable_tile",
            return_value=(2500, 550, 15, 220, False),
        ), patch(
            "anima.procedures.mine_ore.use_on_target",
            new=_use_on_target,
        ), patch(
            "anima.procedures.mine_ore.asyncio.sleep",
            new=AsyncMock(),
        ):
            ctx.bus = None
            result = await proc.execute(ctx)

        assert result.success is True
        # Drop-verify semantics (2026-06-12): the mocked world never moves the
        # ore out of the backpack, so the ground drop reads as BOUNCED — no
        # ground pile may be registered (phantom piles wasted collect walks).
        # The expedition state machine still advances on the mine itself.
        assert len(exp.piles) == 0
        assert exp.phase == Phase.MINING
        assert exp.home_base == (
            ctx.perception.self_state.x, ctx.perception.self_state.y,
        )
        # A VERIFIED ground drop still registers a pile.
        exp2 = MiningExpedition()
        exp2.note_ore_mined(
            ctx.perception.self_state.x, ctx.perception.self_state.y,
            _bank_key(ctx.perception.self_state.x, ctx.perception.self_state.y),
            ground_pile=True,
        )
        assert len(exp2.piles) == 1

    @pytest.mark.asyncio
    async def test_mine_without_expedition_does_not_crash(self):
        """With no 'expedition' key on the blackboard, mining must still work.

        This exercises the hook path end-to-end: execute reaches the
        `ore_gained > 0` branch, the hook runs, and the `if expedition is
        not None` guard prevents an AttributeError on the missing key.
        """
        from unittest.mock import patch, AsyncMock, MagicMock, PropertyMock

        from anima.procedures.mine_ore import MineOre

        proc = MineOre()
        ctx = _make_ctx()
        ctx.blackboard = {}  # No "expedition" key

        before_items, after_items = _setup_mine_success_ctx(ctx)
        after_mine = [False]

        async def _use_on_target(*args, **kwargs):
            after_mine[0] = True
            return MagicMock(success=True, message="ok")

        def _items_getter(self):
            return after_items if after_mine[0] else before_items

        type(ctx.perception.world).items = PropertyMock(side_effect=lambda: _items_getter(None))

        with patch(
            "anima.procedures.mine_ore._find_mineable_tile",
            return_value=(2500, 550, 15, 220, False),
        ), patch(
            "anima.procedures.mine_ore.use_on_target",
            new=_use_on_target,
        ), patch(
            "anima.procedures.mine_ore.asyncio.sleep",
            new=AsyncMock(),
        ):
            ctx.bus = None
            try:
                result = await proc.execute(ctx)
            except AttributeError as e:
                # If the hook accessed .note_ore_mined on None, we'd land here.
                raise AssertionError(
                    f"execute() raised AttributeError — missing None-guard on expedition hook: {e}"
                )

        # We reached the hook path successfully.
        assert result.success is True
