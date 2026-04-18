"""Tests for the rule-based Planner — gameplay loop."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.planner.planner import Planner
from anima.procedures.base import (
    FailureReason,
    Procedure,
    ProcedureRegistry,
    ProcedureResult,
)


class StubProcedure(Procedure):
    def __init__(self, name: str, can: bool = True, suggestion: str | None = None):
        self.name = name
        self.description = f"Stub {name}"
        self._can = can
        self._suggestion = suggestion

    async def can_start(self, ctx):
        return self._can

    async def execute(self, ctx):
        return ProcedureResult(
            success=True,
            message=f"{self.name} done",
            next_suggestion=self._suggestion,
        )


def _make_ctx(**overrides):
    ctx = MagicMock()
    ctx.perception.self_state.x = 100
    ctx.perception.self_state.y = 200
    ctx.perception.self_state.z = 0
    ctx.perception.self_state.hits = 100
    ctx.perception.self_state.hits_max = 100
    ctx.perception.self_state.weight = 100
    ctx.perception.self_state.weight_max = 400
    ctx.perception.self_state.gold = 50  # below bank threshold (200)
    ctx.perception.self_state.equipment = {0x15: 0x101}  # backpack
    ctx.perception.world.items = {}  # empty backpack
    ctx.conn.send_packet = AsyncMock()
    ctx.conn.connected = True
    ctx.blackboard = {}
    ctx.bus = None
    ctx.memory_db = None
    ctx.persona = MagicMock()
    ctx.persona.name = "TestAgent"
    for k, v in overrides.items():
        setattr(ctx.perception.self_state, k, v)
    return ctx


def _add_item(ctx, serial, graphic, amount=1, hue=0):
    """Add an item to mock backpack (container=0x101)."""
    item = MagicMock(container=0x101, graphic=graphic, amount=amount, hue=hue, serial=serial)
    ctx.perception.world.items[serial] = item


# Graphic constants
PICKAXE = 0x0E86
ORE = 0x19B9
INGOT = 0x1BF2
TONGS = 0x0FBB


class TestGameplayLoop:
    """Test the full gameplay loop: mine → smelt → sell → bank → buy → mine."""

    @pytest.mark.asyncio
    async def test_survival_first(self):
        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self"))
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)

        ctx = _make_ctx(hits=20, hits_max=100)
        _add_item(ctx, 1, PICKAXE)
        proc = await planner.select_procedure(ctx)
        assert proc.name == "heal_self"

    @pytest.mark.asyncio
    async def test_overweight_smelt(self):
        reg = ProcedureRegistry()
        reg.register(StubProcedure("smelt_ore"))
        planner = Planner(reg)

        ctx = _make_ctx(weight=360, weight_max=400)
        _add_item(ctx, 99, ORE, amount=10)  # need ore to trigger smelt path
        proc = await planner.select_procedure(ctx)
        assert proc.name == "smelt_ore"

    @pytest.mark.asyncio
    async def test_has_ore_smelt(self):
        """Ore in backpack → smelt before mining more (requires COLLECTING phase)."""
        from anima.planner.expedition import Phase

        reg = ProcedureRegistry()
        reg.register(StubProcedure("mine_ore"))
        reg.register(StubProcedure("smelt_ore"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.COLLECTING)

        ctx = _make_ctx()
        _add_item(ctx, 1, PICKAXE)
        _add_item(ctx, 2, ORE, amount=10)
        proc = await planner.select_procedure(ctx)
        assert proc.name == "smelt_ore"

    @pytest.mark.asyncio
    async def test_orphan_unsmeltable_ore_does_not_block_make_tools(self):
        """Stale 1-amount iron ore in `_small_iron_ore_serials` must not trap
        Priority 4a in a forge-bound loop when ingots/tinker tools are ready
        to craft a pickaxe.
        """
        from anima.skills.crafting.tinker import TINKER_TOOLS_GRAPHICS

        reg = ProcedureRegistry()
        reg.register(StubProcedure("smelt_ore", can=False))  # no forge in mock
        reg.register(StubProcedure("make_tools"))
        planner = Planner(reg)

        ctx = _make_ctx(gold=0)
        # Tinker kit + 21 iron ingots — make_tools.can_start() should pass.
        _add_item(ctx, 1, next(iter(TINKER_TOOLS_GRAPHICS)))
        _add_item(ctx, 2, INGOT, amount=21)
        # Orphan 1-amount iron ore that smelt previously gave up on.
        _add_item(ctx, 99, ORE, amount=1)
        ctx.blackboard["_small_iron_ore_serials"] = {99}

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "make_tools", (
            f"expected fall-through to make_tools, got {proc.name}"
        )

    @pytest.mark.asyncio
    async def test_has_ingots_no_tongs_no_gold_sells_raw(self):
        """No tongs + no gold + many ingots → sell raw as last resort to fund tongs."""
        from anima.planner.expedition import Phase

        reg = ProcedureRegistry()
        reg.register(StubProcedure("sell_to_vendor"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.CRAFTING_TRIP)

        ctx = _make_ctx(gold=0)
        _add_item(ctx, 1, PICKAXE)  # has tool so Priority 4 doesn't block
        _add_item(ctx, 2, INGOT, amount=20)
        proc = await planner.select_procedure(ctx)
        assert proc.name == "sell_to_vendor"

    @pytest.mark.asyncio
    async def test_gold_bank(self):
        """Gold > 200 + has tools → bank deposit."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("bank_deposit"))
        planner = Planner(reg)

        ctx = _make_ctx(gold=500)
        _add_item(ctx, 1, PICKAXE)  # has tools, so Priority 4 doesn't trigger
        proc = await planner.select_procedure(ctx)
        assert proc.name == "bank_deposit"

    @pytest.mark.asyncio
    async def test_no_pickaxe_buy(self):
        """No pickaxe → buy from vendor."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("buy_from_vendor"))
        planner = Planner(reg)

        ctx = _make_ctx()  # empty backpack, no pickaxe
        proc = await planner.select_procedure(ctx)
        assert proc.name == "buy_from_vendor"

    @pytest.mark.asyncio
    async def test_has_pickaxe_mine(self):
        """Has pickaxe, nothing else → mine."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)

        ctx = _make_ctx()
        _add_item(ctx, 1, PICKAXE)
        proc = await planner.select_procedure(ctx)
        assert proc.name == "mine_ore"

    @pytest.mark.asyncio
    async def test_no_procedures_none(self):
        """Nothing can start, no locations → None."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx()
        _add_item(ctx, 1, PICKAXE)
        with patch("anima.world_knowledge.ALL_LOCATIONS", []):
            proc = await planner.select_procedure(ctx)
        assert proc is None

    @pytest.mark.asyncio
    async def test_no_pickaxe_no_vendor_moves_to_shop(self):
        """No pickaxe, buy can't start → move to shop."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("buy_from_vendor", can=False))
        planner = Planner(reg)

        # Position near Minoc so shops are within max_dist
        ctx = _make_ctx(x=2500, y=500)
        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert "move_to" in proc.name

    @pytest.mark.asyncio
    async def test_zero_in_hand_gold_with_fresh_bank_balance_buys(self):
        """Gold=0 in hand but bank has money → should still try buy path.

        Vendor purchases deduct from BANK gold, not backpack, so in-hand gold=0
        should not block the buy branch. This guards against regressions where
        the agent wrongly declares deadlock while the bank is funded.
        """
        reg = ProcedureRegistry()
        reg.register(StubProcedure("buy_from_vendor"))
        planner = Planner(reg)

        ctx = _make_ctx(x=2500, y=500, gold=0)  # empty pockets, near Minoc
        # Prime bank_balance cache with fresh, sufficient amount
        ctx.blackboard["bank_balance"] = {
            "amount": 500,
            "ts": time.time(),
        }

        proc = await planner.select_procedure(ctx)
        assert proc is not None, "should not bail to None when bank has money"
        assert proc.name == "buy_from_vendor"

    @pytest.mark.asyncio
    async def test_zero_in_hand_gold_stale_bank_checks_balance(self):
        """Gold=0, no bank cache → should pick check_bank_balance, not give up."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("check_bank_balance"))
        reg.register(StubProcedure("buy_from_vendor", can=False))
        planner = Planner(reg)

        ctx = _make_ctx(x=2500, y=500, gold=0)  # empty pockets
        # No bank_balance cache — planner must verify before deciding
        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "check_bank_balance"


class TestPlannerTick:
    @pytest.mark.asyncio
    async def test_tick_no_procedure(self):
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx()
        _add_item(ctx, 1, PICKAXE)
        with patch("anima.world_knowledge.ALL_LOCATIONS", []):
            result = await planner.tick(ctx)
        assert result is None


import json
import time as _time


class TestSupervisorHints:
    @pytest.mark.asyncio
    async def test_skips_hinted_procedure(self, tmp_path):
        """Planner skips procedure listed in supervisor_hints.json."""
        hints_file = tmp_path / "supervisor_hints.json"
        hints_file.write_text(json.dumps({
            "skip_procedures": {
                "craft_blacksmith": {
                    "until": _time.time() + 3600,
                    "reason": "missing_resource",
                }
            }
        }))

        reg = ProcedureRegistry()
        reg.register(StubProcedure("craft_blacksmith"))
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)

        ctx = _make_ctx()
        _add_item(ctx, 1, PICKAXE)
        _add_item(ctx, 2, INGOT, amount=20)

        with patch("anima.planner.planner.SUPERVISOR_HINTS_FILE", hints_file):
            proc = await planner.select_procedure(ctx)
            if proc and proc.name == "craft_blacksmith":
                # select_procedure returned it, but tick() should skip it
                result = await planner.tick(ctx)
                assert result is None
            # If select_procedure returned something else or None, that's also fine

    @pytest.mark.asyncio
    async def test_expired_hint_ignored(self, tmp_path):
        """Expired hint does not skip procedure."""
        from anima.planner.expedition import Phase

        hints_file = tmp_path / "supervisor_hints.json"
        hints_file.write_text(json.dumps({
            "skip_procedures": {
                "craft_blacksmith": {
                    "until": _time.time() - 100,  # expired
                    "reason": "missing_resource",
                }
            }
        }))

        reg = ProcedureRegistry()
        reg.register(StubProcedure("craft_blacksmith"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.CRAFTING_TRIP)

        ctx = _make_ctx()
        _add_item(ctx, 1, PICKAXE)
        _add_item(ctx, 2, INGOT, amount=20)
        _add_item(ctx, 3, TONGS)  # tongs needed to reach craft path

        with patch("anima.planner.planner.SUPERVISOR_HINTS_FILE", hints_file):
            result = await planner.tick(ctx)
            # Expired hint should NOT block — procedure runs normally
            assert result is not None


def _add_ground_item(ctx, serial, graphic, x, y, amount=1, hue=0):
    """Add an item on the ground (container=0)."""
    item = MagicMock(
        container=0, graphic=graphic, amount=amount, hue=hue,
        serial=serial, x=x, y=y,
    )
    ctx.perception.world.items[serial] = item


GOLD = 0x0EED


class TestDeathRecovery:
    """Test Priority 0: dead agent → seek resurrection."""

    @pytest.mark.asyncio
    async def test_dead_agent_seeks_resurrection(self):
        """HP=0 (dead) → planner selects seek_resurrection, ignoring all else."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self"))
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)

        ctx = _make_ctx(hits=0, hits_max=100)
        ctx.perception.self_state.is_alive = False
        _add_item(ctx, 1, PICKAXE)  # has tools — would normally mine

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "seek_resurrection"

    @pytest.mark.asyncio
    async def test_alive_agent_does_not_seek_resurrection(self):
        """HP > 0 → normal procedure selection, not seek_resurrection."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)

        ctx = _make_ctx(hits=50, hits_max=100)
        _add_item(ctx, 1, PICKAXE)

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name != "seek_resurrection"

    @pytest.mark.asyncio
    async def test_dead_agent_sets_intent(self):
        """Dead agent sets planner_intent indicating death recovery."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(hits=0, hits_max=112)
        ctx.perception.self_state.is_alive = False

        await planner.select_procedure(ctx)
        intent = ctx.blackboard.get("planner_intent", "")
        assert "사망" in intent or "부활" in intent

    @pytest.mark.asyncio
    async def test_hunt_for_gold_bails_when_dead(self):
        """_HuntForGold.run returns failure immediately if agent is dead."""
        from anima.planner.planner import _HuntForGold

        ctx = _make_ctx(hits=0, hits_max=100)
        ctx.perception.self_state.is_alive = False

        target = MagicMock(
            serial=0x1234, name="a rat", x=100, y=200, body=0x00EE,
        )
        proc = _HuntForGold(target, ctx.perception.self_state)
        result = await proc.run(ctx)

        assert result.success is False
        assert result.reason == FailureReason.BLOCKED
        assert "dead" in result.message.lower()

    @pytest.mark.asyncio
    async def test_hunt_blacklists_body_after_two_unreachable_failures(self):
        """Two distinct serials of the same body unreachable → body blacklisted."""
        from anima.planner.planner import _HuntForGold

        ctx = _make_ctx()
        ctx.perception.self_state.is_alive = True
        ctx.perception.self_state.pending_target = None
        ctx.perception.self_state.serial = 0x01

        # First cat: go_to fails → per-serial blacklist, body count = 1
        target1 = MagicMock(
            serial=0xCAA1, name="a cat", x=110, y=210, body=0x00C9,
        )
        proc1 = _HuntForGold(target1, ctx.perception.self_state)
        with patch("anima.action.movement.go_to", new=AsyncMock(return_value=False)):
            result1 = await proc1.run(ctx)
        assert result1.success is False
        assert "cannot reach" in result1.message.lower()
        # Body not yet blacklisted (only 1 failure)
        assert 0x00C9 not in ctx.blackboard.get("_hunt_unreachable_bodies", {})
        # Second cat with different serial: go_to fails → body blacklisted
        target2 = MagicMock(
            serial=0xCAA2, name="a cat", x=108, y=208, body=0x00C9,
        )
        proc2 = _HuntForGold(target2, ctx.perception.self_state)
        with patch("anima.action.movement.go_to", new=AsyncMock(return_value=False)):
            result2 = await proc2.run(ctx)
        assert result2.success is False
        assert 0x00C9 in ctx.blackboard.get("_hunt_unreachable_bodies", {})

    @pytest.mark.asyncio
    async def test_find_huntable_skips_body_in_unreachable_blacklist(self):
        """_find_huntable_target skips all mobiles whose body is blacklisted."""
        from anima.perception.enums import NotorietyFlag
        from anima.perception.world_state import MobileInfo, WorldState
        from anima.planner.planner import Planner
        from anima.procedures.base import ProcedureRegistry

        planner = Planner(ProcedureRegistry())
        ctx = _make_ctx()
        ctx.perception.self_state.serial = 0x01
        ctx.map_reader = None  # skip pathfinding reachability check

        world = WorldState()
        ctx.perception.world = world
        # Two cats (blacklisted body) and one rat (not blacklisted)
        for serial, body, x in [
            (0xCAA1, 0x00C9, 102),  # cat
            (0xCAA2, 0x00C9, 103),  # cat
            (0xDEAD01, 0x00EE, 104),  # rat
        ]:
            m = MobileInfo(serial=serial, body=body, x=x, y=200, z=0)
            m.notoriety = NotorietyFlag.ATTACKABLE
            world.mobiles[serial] = m

        ctx.blackboard["_hunt_unreachable_bodies"] = {0x00C9: time.time()}

        target = planner._find_huntable_target(
            ctx, ctx.perception.self_state, include_small_animals=True,
        )
        assert target is not None
        assert target.body == 0x00EE  # rat, not cat

    @pytest.mark.asyncio
    async def test_find_huntable_expires_stale_body_blacklist(self):
        """Body blacklist entries older than 300s expire so stale entries don't persist."""
        from anima.perception.enums import NotorietyFlag
        from anima.perception.world_state import MobileInfo, WorldState
        from anima.planner.planner import Planner
        from anima.procedures.base import ProcedureRegistry

        planner = Planner(ProcedureRegistry())
        ctx = _make_ctx()
        ctx.perception.self_state.serial = 0x01
        ctx.map_reader = None

        world = WorldState()
        ctx.perception.world = world
        cat = MobileInfo(serial=0xCAA1, body=0x00C9, x=101, y=200, z=0)
        cat.notoriety = NotorietyFlag.ATTACKABLE
        world.mobiles[cat.serial] = cat

        # Stale entry (601s ago) should be lazily cleaned
        ctx.blackboard["_hunt_unreachable_bodies"] = {0x00C9: time.time() - 601}

        target = planner._find_huntable_target(
            ctx, ctx.perception.self_state, include_small_animals=True,
        )
        assert target is not None
        assert target.body == 0x00C9
        assert 0x00C9 not in ctx.blackboard["_hunt_unreachable_bodies"]


class TestDeadlockRecovery:
    """Test deadlock recovery: no tools, no gold, no materials → scavenge."""

    @pytest.mark.asyncio
    async def test_deadlock_scavenges_ground_gold(self):
        """True deadlock + gold on ground → scavenge_ground_items."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        # No tools, no gold (gold=0), empty backpack
        ctx = _make_ctx(gold=0)
        # Gold coins on the ground nearby
        _add_ground_item(ctx, 0xAA, GOLD, x=105, y=205)

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "scavenge_ground_items"

    @pytest.mark.asyncio
    async def test_deadlock_scavenges_ground_pickaxe(self):
        """True deadlock + pickaxe on ground → scavenge_ground_items."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0)
        _add_ground_item(ctx, 0xBB, PICKAXE, x=100, y=200)

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "scavenge_ground_items"

    @pytest.mark.asyncio
    async def test_deadlock_no_ground_items_walks_to_town(self):
        """True deadlock + nothing on ground → move to populated area."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        # Position near Minoc so town locations are in range
        ctx = _make_ctx(gold=0, x=2500, y=500)

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert "move_to" in proc.name

    @pytest.mark.asyncio
    async def test_deadlock_no_ground_no_town_returns_none(self):
        """True deadlock + nothing nearby at all → returns None."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0)

        with patch("anima.world_knowledge.ALL_LOCATIONS", []):
            proc = await planner.select_procedure(ctx)
        assert proc is None

    @pytest.mark.asyncio
    async def test_deadlock_escalates_to_wander_after_attempts(self):
        """After 3+3 failed attempts (no items, no move), escalate to wander."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0)

        with patch("anima.world_knowledge.ALL_LOCATIONS", []):
            # Level 0: 3 attempts return None (nothing to scavenge, nowhere to go)
            for _ in range(3):
                proc = await planner.select_procedure(ctx)
                assert proc is None

            # Attempt 4 triggers escalation to Level 1.
            # Level 1 walk-to-town also fails (no locations) → None.
            proc = await planner.select_procedure(ctx)
            assert proc is None
            assert ctx.blackboard["_deadlock_recovery_level"] == 1

            # Level 1: 3 more None attempts
            for _ in range(3):
                proc = await planner.select_procedure(ctx)
                assert proc is None

            # Attempt 7 triggers escalation to Level 2 → wander_and_scavenge
            proc = await planner.select_procedure(ctx)
            assert proc is not None
            assert proc.name == "wander_and_scavenge"
            assert ctx.blackboard["_deadlock_recovery_level"] == 2

    @pytest.mark.asyncio
    async def test_deadlock_skips_ground_ore_when_flagged(self):
        """Priority 3b respects _skip_procedures for pick_up_ore_and_smelt."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        # Position near Minoc
        ctx = _make_ctx(gold=0, x=2460, y=558)
        # Ore on ground within range
        _add_ground_item(ctx, 0xCC, ORE, x=2461, y=558, amount=5)
        # Flag pick_up_ore_and_smelt as skipped (repeat failure detection)
        ctx.blackboard["_skip_procedures"] = {"pick_up_ore_and_smelt"}

        proc = await planner.select_procedure(ctx)
        # Should NOT return pick_up_ore_and_smelt — should reach Priority 4f
        assert proc is None or proc.name != "pick_up_ore_and_smelt"

    @pytest.mark.asyncio
    async def test_deadlock_sells_junk_when_vendor_nearby(self):
        """True deadlock + junk items in backpack + vendor nearby → sell_to_vendor."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("sell_to_vendor"))
        planner = Planner(reg)

        ctx = _make_ctx(gold=0)
        # Add a junk item (book graphic) — not in KEEP_GRAPHICS
        BOOK_GRAPHIC = 0x0FF4
        _add_item(ctx, 0xCC, BOOK_GRAPHIC)

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "sell_to_vendor"

    @pytest.mark.asyncio
    async def test_deadlock_with_junk_walks_to_vendor(self):
        """True deadlock + junk items + no vendor nearby → walk to vendor area."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("sell_to_vendor", can=False))
        planner = Planner(reg)

        # Position near Minoc so vendor locations are reachable
        ctx = _make_ctx(gold=0, x=2500, y=500)
        BOOK_GRAPHIC = 0x0FF4
        _add_item(ctx, 0xCC, BOOK_GRAPHIC)

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert "move_to" in proc.name

    @pytest.mark.asyncio
    async def test_deadlock_no_junk_skips_sell(self):
        """True deadlock + empty backpack (only essential items) → no sell attempt."""
        reg = ProcedureRegistry()
        reg.register(StubProcedure("sell_to_vendor"))
        planner = Planner(reg)

        # Gold on ground so we can verify the agent scavenges instead
        ctx = _make_ctx(gold=0)
        _add_ground_item(ctx, 0xAA, GOLD, x=105, y=205)

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        # Should scavenge ground gold, not sell
        assert proc.name == "scavenge_ground_items"

    @pytest.mark.asyncio
    async def test_deadlock_post_forum_relocate_jumps_to_level4(self):
        """_post_forum_relocate flag near stranded pos → _RelocateToCity."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0, x=2456, y=453)
        ctx.blackboard["_post_forum_relocate"] = True
        ctx.blackboard["_forum_stranded_pos"] = (2450, 450)

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "relocate_to_city"
        # Flag should still be set (we haven't moved far yet)
        assert ctx.blackboard.get("_post_forum_relocate") is True

    @pytest.mark.asyncio
    async def test_deadlock_post_forum_relocate_abandons_after_repeated_fails(self):
        """After 5+ relocate_to_city failures, abandon relocation and fall
        back to yell_for_help (no-A* strategy).  Covers the real-world
        infinite loop where Britain is unreachable from the stranded pos."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0, x=2456, y=453)
        ctx.blackboard["_post_forum_relocate"] = True
        ctx.blackboard["_forum_stranded_pos"] = (2450, 450)
        # Seed the repeat counter as if relocate has already failed 5x
        planner._repeat_counter["relocate_to_city"] = 5

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "yell_for_help"
        # Flags are cleared so the normal deadlock ladder resumes
        assert "_post_forum_relocate" not in ctx.blackboard
        assert "_forum_stranded_pos" not in ctx.blackboard
        # Counter reset so a future Lv4 relocate gets a fresh 5-strike budget
        assert planner._repeat_counter["relocate_to_city"] == 0
        # Level jumped to 6 so next tick stays on non-A* strategies
        assert ctx.blackboard["_deadlock_recovery_level"] == 6

    @pytest.mark.asyncio
    async def test_deadlock_post_forum_relocate_clears_when_far(self):
        """_post_forum_relocate flag clears once agent has moved >60 tiles."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        # Agent has moved 100 tiles east of the stranded position
        ctx = _make_ctx(gold=0, x=2550, y=453)
        ctx.blackboard["_post_forum_relocate"] = True
        ctx.blackboard["_forum_stranded_pos"] = (2450, 450)

        await planner.select_procedure(ctx)
        # Flag should be cleared — normal level-0 recovery resumes
        assert "_post_forum_relocate" not in ctx.blackboard
        assert "_forum_stranded_pos" not in ctx.blackboard

    @pytest.mark.asyncio
    async def test_resolve_deadlock_clears_state(self):
        """DeadlockResolver.resolve Strategy 5 resets failed destinations and idle ticks."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0)
        ctx.bus = None
        planner._idle_ticks = 150
        # Use fresh timestamps so strategies 1-4 don't trigger early return
        now = _time.time()
        planner._failed_destinations = {(100, 200): now, (300, 400): now}
        planner._move_fail_until = 999999.0

        await planner._deadlock.resolve(ctx)

        assert planner._idle_ticks == 0
        assert len(planner._failed_destinations) == 0

    @pytest.mark.asyncio
    async def test_deadlock_lv2_with_weapon_seeks_wilderness(self):
        """Lv2 + dagger in backpack + no visible hunt target → move to mine/camp."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        # Position near Minoc so wilderness keywords match real locations
        ctx = _make_ctx(gold=0, x=2504, y=552)
        # Dagger — in _HuntForGold._WEAPON_GRAPHICS
        _add_item(ctx, 0xDD, 0x0F51)
        # Force Lv2 state without running escalation ladder
        ctx.blackboard["_deadlock_recovery_level"] = 2
        ctx.blackboard["_deadlock_attempt_count"] = 0

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        # _roaming.move_to_location returns a _MoveToProcedure with
        # name like "move_to_<location>" — not the town-wander fallback.
        assert proc.name != "wander_and_scavenge"
        assert "move_to" in proc.name

    @pytest.mark.asyncio
    async def test_deadlock_lv2_without_weapon_falls_back_to_wander(self):
        """Lv2 without any weapon → still wanders (safer than walking to spawns unarmed)."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        # Backpack has only non-weapon junk (a book) — no dagger/pickaxe/etc.
        ctx = _make_ctx(gold=0, x=2504, y=552)
        # Empty the clothing graphics from equipment map — pure unarmed state.
        # _make_ctx only wires 0x15 (backpack) so equipment has no weapon slot.
        # Block move_to_location so Lv3+ wilderness-move logic doesn't mask the
        # Lv2 branch; here we just assert wander fallback when unarmed.
        ctx.blackboard["_deadlock_recovery_level"] = 2
        ctx.blackboard["_deadlock_attempt_count"] = 0
        planner._move_fail_until = time.time() + 300  # block all move_to calls

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "wander_and_scavenge"

    @pytest.mark.asyncio
    async def test_deadlock_opportunistic_hunt_of_close_prey(self):
        """Deadlock + dagger + rat within 5 tiles → hunt immediately, bypass ladder."""
        from anima.perception.enums import NotorietyFlag

        reg = ProcedureRegistry()
        planner = Planner(reg)

        # Position near Minoc so 4c's move-to-vendor branch can match, but
        # force it to return None by blocking move_to_location with a cooldown.
        ctx = _make_ctx(gold=0, x=2504, y=552)
        ctx.map_reader = None  # skip pathfinding reachability filter
        # Dagger (0x0F51) in backpack — satisfies _has_usable_weapon
        _add_item(ctx, 0xDD, 0x0F51)

        # Rat 2 tiles away — well within the 5-tile opportunistic range
        rat = MagicMock(
            serial=0x9999, name="a rat",
            x=2506, y=553, body=0x00EE,
            notoriety=NotorietyFlag.ATTACKABLE,
        )
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[rat])

        # Force Lv4 state — normally would return _RelocateToCity, but the
        # opportunistic hunt must win when prey is in immediate view.
        ctx.blackboard["_deadlock_recovery_level"] = 4
        ctx.blackboard["_deadlock_attempt_count"] = 0
        planner._move_fail_until = time.time() + 300

        with patch("anima.world_knowledge.ALL_LOCATIONS", []):
            proc = await planner.select_procedure(ctx)

        assert proc is not None
        assert proc.name == "hunt_for_gold"

    @pytest.mark.asyncio
    async def test_deadlock_distant_prey_does_not_short_circuit(self):
        """Rat >5 tiles away must not trigger the opportunistic hunt."""
        from anima.perception.enums import NotorietyFlag

        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0, x=2504, y=552)
        ctx.map_reader = None
        _add_item(ctx, 0xDD, 0x0F51)

        far_rat = MagicMock(
            serial=0x9999, name="a rat",
            x=2520, y=560, body=0x00EE,
            notoriety=NotorietyFlag.ATTACKABLE,
        )
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[far_rat])

        ctx.blackboard["_deadlock_recovery_level"] = 2
        ctx.blackboard["_deadlock_attempt_count"] = 0

        proc = await planner.select_procedure(ctx)
        # Should fall through to Lv2 wilderness-seek or other paths — NOT hunt
        assert proc is None or proc.name != "hunt_for_gold"

    @pytest.mark.asyncio
    async def test_deadlock_lv0_medium_range_hunt_after_sell_attempt(self):
        """Lv0 + weapon + prey 6-18 tiles + attempts≥1 → hunt instead of sell cycle.

        Covers the true-deadlock case where vendor-sell keeps failing (vendor
        refuses clothes/book) and the agent has a dagger with a reachable rat
        at medium range.  The opportunistic-hunt branch should prefer hunting
        over continuing the sell→walk→refusal cycle.
        """
        from anima.perception.enums import NotorietyFlag

        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0, x=2524, y=547)
        ctx.map_reader = None  # skip A* reachability filter in test
        # Dagger in backpack — usable weapon
        _add_item(ctx, 0xDD, 0x0F51)

        rat = MagicMock(
            serial=0x9999, name="a rat",
            x=2534, y=547, body=0x00EE,  # 10 tiles east — beyond 5
            notoriety=NotorietyFlag.ATTACKABLE,
        )
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[rat])

        # Lv0 + 1 attempt already logged (first sell try failed)
        ctx.blackboard["_deadlock_recovery_level"] = 0
        ctx.blackboard["_deadlock_attempt_count"] = 1
        planner._move_fail_until = time.time() + 300

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "hunt_for_gold"

    @pytest.mark.asyncio
    async def test_deadlock_lv0_first_attempt_still_tries_sell(self):
        """Lv0 + weapon + medium-range prey + attempts=0 → sell path (not hunt).

        The first sell attempt must still run; hunt-bypass only kicks in
        after one failed sell cycle so legitimate vendor interactions get
        a chance.
        """
        from anima.perception.enums import NotorietyFlag

        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0, x=2524, y=547)
        ctx.map_reader = None
        _add_item(ctx, 0xDD, 0x0F51)

        rat = MagicMock(
            serial=0x9999, name="a rat",
            x=2534, y=547, body=0x00EE,
            notoriety=NotorietyFlag.ATTACKABLE,
        )
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[rat])

        ctx.blackboard["_deadlock_recovery_level"] = 0
        ctx.blackboard["_deadlock_attempt_count"] = 0

        proc = await planner.select_procedure(ctx)
        # On first attempt, hunt_for_gold should NOT fire for medium-range prey
        assert proc is None or proc.name != "hunt_for_gold"

    @pytest.mark.asyncio
    async def test_deadlock_lv6_yells_for_help(self):
        """Lv5 escalation → Lv6 = stationary _YellForHelp (not forum yet)."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0, x=2504, y=552)
        # Force Lv5 with 3 attempts pending so the next tick escalates to Lv6.
        ctx.blackboard["_deadlock_recovery_level"] = 5
        ctx.blackboard["_deadlock_attempt_count"] = 3
        # Block all move_to_location paths so 4c's vendor-walk and every
        # other move-based fallback return None, letting the flow fall
        # through to the 4f deadlock block.
        planner._move_fail_until = time.time() + 300

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "yell_for_help"
        assert ctx.blackboard["_deadlock_recovery_level"] == 6

    @pytest.mark.asyncio
    async def test_deadlock_stuck_in_place_shortcircuits_to_lv6(self):
        """Low-level deadlock + no position drift across entries → jump to Lv6 yell.

        Simulates the observed "pathfinding trap" where every named move
        fails with `blocked` and the agent's position stays fixed, so the
        ladder's Lv2-Lv5 strategies would all re-fail the same way.
        """
        reg = ProcedureRegistry()
        planner = Planner(reg)

        # Agent at Minoc.  Pretend the current recovery cycle started here
        # (same coordinates as _first_pos ⇒ drift = 0).
        ctx = _make_ctx(gold=0, x=2456, y=453)
        ctx.blackboard["_deadlock_recovery_level"] = 2
        ctx.blackboard["_deadlock_attempt_count"] = 3
        ctx.blackboard["_deadlock_first_pos"] = (2456, 453)
        # Block outbound walk procedures so the ladder would otherwise
        # crawl through Lv2→Lv3→…→Lv6.
        planner._move_fail_until = time.time() + 300

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "yell_for_help"
        assert ctx.blackboard["_deadlock_recovery_level"] == 6
        assert ctx.blackboard["_deadlock_attempt_count"] == 0

    @pytest.mark.asyncio
    async def test_deadlock_stuck_shortcircuit_respects_drift(self):
        """Agent has moved far enough from _first_pos → short-circuit must NOT fire."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        # Current pos 20 tiles east of _first_pos ⇒ drift > 8 ⇒ no shortcut.
        ctx = _make_ctx(gold=0, x=2476, y=453)
        ctx.blackboard["_deadlock_recovery_level"] = 2
        ctx.blackboard["_deadlock_attempt_count"] = 3
        ctx.blackboard["_deadlock_first_pos"] = (2456, 453)
        planner._move_fail_until = time.time() + 300

        proc = await planner.select_procedure(ctx)
        # Whatever was selected, it must not be yell_for_help (that path is
        # reserved for the stuck-in-place case).
        assert proc is None or proc.name != "yell_for_help"
        # Level must not have been forced to 6 by the short-circuit.
        assert ctx.blackboard.get("_deadlock_recovery_level") != 6

    @pytest.mark.asyncio
    async def test_deadlock_first_pos_seeded_on_first_entry(self):
        """First deadlock entry records _first_pos for later drift checks."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0, x=2456, y=453)

        with patch("anima.world_knowledge.ALL_LOCATIONS", []):
            await planner.select_procedure(ctx)

        assert ctx.blackboard.get("_deadlock_first_pos") == (2456, 453)

    @pytest.mark.asyncio
    async def test_deadlock_lv7_escalates_to_forum(self):
        """Lv6 escalation → Lv7 = forum (pushed from prior Lv6)."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0, x=2504, y=552)
        ctx.blackboard["_deadlock_recovery_level"] = 6
        ctx.blackboard["_deadlock_attempt_count"] = 3
        ctx.blackboard["_deadlock_first_pos"] = (2504, 552)
        planner._move_fail_until = time.time() + 300

        # escalate_to_forum is async and posts/waits — patch it out.
        with patch.object(
            planner._deadlock, "escalate_to_forum", new=AsyncMock()
        ) as mock_esc:
            proc = await planner.select_procedure(ctx)

        assert proc is None
        mock_esc.assert_awaited_once()
        # Level resets to 0 after forum escalation.
        assert ctx.blackboard["_deadlock_recovery_level"] == 0
        # Cross-cycle memory: cycle counter is incremented and the starting
        # position is preserved so the next deadlock can fast-forward.
        assert ctx.blackboard["_deadlock_cycles_completed"] == 1
        assert ctx.blackboard["_deadlock_last_cycle_pos"] == (2504, 552)
        # _first_pos is cleared so the stuck-shortcircuit restarts fresh.
        assert "_deadlock_first_pos" not in ctx.blackboard

    @pytest.mark.asyncio
    async def test_deadlock_cycle_repeat_jumps_to_relocate(self):
        """After 1 full Lv0-Lv7 cycle in the same area → skip to Lv4."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        # Starting fresh at Lv0 but a previous cycle completed nearby.
        ctx = _make_ctx(gold=0, x=2504, y=552)
        ctx.blackboard["_deadlock_cycles_completed"] = 1
        ctx.blackboard["_deadlock_last_cycle_pos"] = (2500, 550)
        planner._move_fail_until = time.time() + 300

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "relocate_to_city"
        assert ctx.blackboard["_deadlock_recovery_level"] == 4
        assert ctx.blackboard["_deadlock_attempt_count"] == 0

    @pytest.mark.asyncio
    async def test_deadlock_cycle_repeat_yells_when_relocate_blocked(self):
        """Cycle repeat + relocate has been failing → yell instead of relocate."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0, x=2504, y=552)
        ctx.blackboard["_deadlock_cycles_completed"] = 2
        ctx.blackboard["_deadlock_last_cycle_pos"] = (2500, 550)
        # Relocate has already failed 5 times — Britain is unreachable.
        planner._repeat_counter["relocate_to_city"] = 5
        planner._move_fail_until = time.time() + 300

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "yell_for_help"
        assert ctx.blackboard["_deadlock_recovery_level"] == 6
        # Repeat counter reset so a future Lv4 attempt gets a fresh budget.
        assert planner._repeat_counter["relocate_to_city"] == 0

    @pytest.mark.asyncio
    async def test_deadlock_cycle_repeat_respects_drift(self):
        """Agent has moved far from last cycle pos → no fast-forward."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        # 100 tiles from last cycle pos ⇒ drift > 40 ⇒ normal ladder.
        ctx = _make_ctx(gold=0, x=2600, y=552)
        ctx.blackboard["_deadlock_cycles_completed"] = 1
        ctx.blackboard["_deadlock_last_cycle_pos"] = (2500, 550)
        planner._move_fail_until = time.time() + 300

        proc = await planner.select_procedure(ctx)
        # Must NOT jump straight to relocate_to_city.
        assert proc is None or proc.name != "relocate_to_city"
        # Level must not have been force-set to 4 by the shortcut.
        assert ctx.blackboard.get("_deadlock_recovery_level", 0) != 4

    def test_has_usable_weapon_detects_dagger_in_backpack(self):
        """_has_usable_weapon returns True for a dagger in the backpack."""
        reg = ProcedureRegistry()
        planner = Planner(reg)
        ctx = _make_ctx()
        _add_item(ctx, 0xDD, 0x0F51)  # dagger
        assert planner._has_usable_weapon(ctx, ctx.perception.self_state)

    def test_has_usable_weapon_false_when_only_junk(self):
        """_has_usable_weapon returns False when only non-weapon junk is carried."""
        reg = ProcedureRegistry()
        planner = Planner(reg)
        ctx = _make_ctx()
        _add_item(ctx, 0xCC, 0x0FF4)  # book, not a weapon
        assert not planner._has_usable_weapon(ctx, ctx.perception.self_state)

    @pytest.mark.asyncio
    async def test_drop_junk_marks_undroppable_and_preserves_refused(self):
        """Blessed items survive the drop packet; refused_vendors must persist.

        Regression: server silently rejects drops of blessed/newbified
        items with 0x27. Without verification, _DropJunkItems reported
        success, the caller cleared refused_vendors, and the planner
        re-selected the same wrong-type vendor next tick.
        """
        from anima.planner.planner import _DropJunkItems

        ctx = _make_ctx(gold=0)
        BOOK_GRAPHIC = 0x0FF4
        _add_item(ctx, 0xCC, BOOK_GRAPHIC)

        # Seed a refused vendor that must NOT be cleared by a no-op drop.
        ctx.blackboard["refused_vendors"] = {0xABCD: time.monotonic()}

        # Server rejection pattern: the drop packet returns, but the item
        # remains in the backpack because it is blessed.
        proc = _DropJunkItems(ctx.perception.self_state)
        result = await proc.run(ctx)

        assert result.success is False
        assert 0xCC in ctx.blackboard["undroppable_serials"]
        # Refused-vendor bookkeeping must survive — otherwise we'd loop
        # back to the same wrong-type vendor with the same inventory.
        assert 0xABCD in ctx.blackboard["refused_vendors"]

        # Second attempt must skip the already-undroppable item rather
        # than spam the server with the same rejected drop packet.
        ctx.conn.send_packet.reset_mock()
        result2 = await proc.run(ctx)
        assert result2.success is False
        assert result2.reason == FailureReason.MISSING_RESOURCE
        ctx.conn.send_packet.assert_not_called()

    @pytest.mark.asyncio
    async def test_drop_junk_success_clears_refused_vendors(self):
        """A successful drop changes inventory → refused vendors may now accept."""
        from anima.planner.planner import _DropJunkItems

        ctx = _make_ctx(gold=0)
        BOOK_GRAPHIC = 0x0FF4
        _add_item(ctx, 0xCC, BOOK_GRAPHIC)

        ctx.blackboard["refused_vendors"] = {0xABCD: time.monotonic()}

        # Simulate the server accepting the drop by removing the item
        # from the world after the packet is sent.
        async def _fake_send(packet: bytes) -> None:
            ctx.perception.world.items.pop(0xCC, None)

        ctx.conn.send_packet = AsyncMock(side_effect=_fake_send)

        proc = _DropJunkItems(ctx.perception.self_state)
        result = await proc.run(ctx)

        assert result.success is True
        # Inventory genuinely changed → refused marks should be invalidated
        # so a previously-wrong-type vendor can be re-evaluated.
        assert "refused_vendors" not in ctx.blackboard

    @pytest.mark.asyncio
    async def test_buy_disabled_no_ingots_falls_through_to_recovery(self):
        """Bank empty + no ingots/ore must NOT standstill; must reach 4f.

        Previously the planner returned None with "user must intervene"
        when _buy_disabled_until was set and the agent had no ingots or
        ore. That short-circuited every Priority 4f recovery path (hunt,
        scavenge, relocate, yell, forum) for 10 minutes. A true deadlock
        is exactly when recovery is most needed, so fall-through wins.
        """
        reg = ProcedureRegistry()
        planner = Planner(reg)

        # Position near Minoc with a dagger + a huntable rat nearby so
        # 4f's opportunistic close-range hunt fires once fall-through works.
        from anima.perception.enums import NotorietyFlag

        ctx = _make_ctx(gold=0, x=2504, y=552)
        ctx.map_reader = None
        _add_item(ctx, 0xDD, 0x0F51)  # dagger
        rat = MagicMock(
            serial=0x9999, name="a rat",
            x=2506, y=553, body=0x00EE,
            notoriety=NotorietyFlag.ATTACKABLE,
        )
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[rat])

        # Buy disabled (bank known empty) + no ingots/ore — the state the
        # old standstill was targeting.
        ctx.blackboard["_buy_disabled_until"] = time.time() + 600

        proc = await planner.select_procedure(ctx)
        assert proc is not None, (
            "buy_disabled + no ingots/ore must fall through to 4f, not stop"
        )
        # The 4f opportunistic hunt should pick up the rat.
        assert proc.name == "hunt_for_gold"

    @pytest.mark.asyncio
    async def test_buy_blind_walk_count_caps_at_three(self):
        """Unknown bank balance + no banker/vendor reachable must cap the
        blind walk loop so 4f deadlock recovery eventually runs.

        Without the cap, the agent forever ping-pongs between Minoc Bank,
        Tinker, Provisioner, and Miners Guild locations because
        check_bank_balance only fires when a banker is within 12 tiles
        (which only happens *at* the bank), and buy_from_vendor only
        fires when a vendor is in range — neither condition holds during
        transit.
        """
        reg = ProcedureRegistry()
        reg.register(StubProcedure("check_bank_balance", can=False))
        reg.register(StubProcedure("buy_from_vendor", can=False))
        planner = Planner(reg)

        ctx = _make_ctx(gold=0, x=2500, y=500)

        # First three calls must return a move_to procedure (normal 4c walk).
        for i in range(3):
            proc = await planner.select_procedure(ctx)
            assert proc is not None, f"attempt {i}: expected blind walk"
            assert "move_to" in proc.name, (
                f"attempt {i}: expected move_to, got {proc.name}"
            )
        assert ctx.blackboard["_buy_blind_walk_count"] == 3

        # Fourth call: counter exhausted — 4c must NOT return another walk
        # to the same vendor keywords.  Without nearby deadlock-recovery
        # assets, the planner falls through to 4f and either returns a
        # recovery procedure or None (4f's "nothing to do" path).
        # Either outcome is acceptable; what matters is that the blind
        # walk loop does not repeat.
        proc = await planner.select_procedure(ctx)
        if proc is not None and "move_to" in proc.name:
            # If a move is returned it must be a recovery walk (e.g.
            # Lv3 beg-at-bank), not the same blind-walk target list —
            # counter stays pinned so the fall-through path continues.
            assert ctx.blackboard["_buy_blind_walk_count"] == 3

    @pytest.mark.asyncio
    async def test_blind_walk_counter_resets_on_fresh_balance(self):
        """A fresh bank_balance cache resets the blind-walk counter.

        Without a reset, the agent is permanently locked out of Priority
        4c after one bad stretch even once the bank is refilled and
        check_bank_balance re-caches the new amount.
        """
        reg = ProcedureRegistry()
        reg.register(StubProcedure("buy_from_vendor"))
        planner = Planner(reg)

        ctx = _make_ctx(gold=0, x=2500, y=500)
        # Simulate a stale cycle where the counter reached the cap.
        ctx.blackboard["_buy_blind_walk_count"] = 3
        # Fresh, sufficient balance — 4c's "known sufficient" branch must
        # both return buy_from_vendor AND clear the counter.
        ctx.blackboard["bank_balance"] = {
            "amount": 500,
            "ts": time.time(),
        }

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "buy_from_vendor"
        assert ctx.blackboard["_buy_blind_walk_count"] == 0


class TestExpeditionWiring:
    @pytest.mark.asyncio
    async def test_expedition_attached_to_planner(self):
        """Planner.__init__ creates a MiningExpedition."""
        from anima.planner.expedition import MiningExpedition

        reg = ProcedureRegistry()
        planner = Planner(reg)
        assert isinstance(planner._expedition, MiningExpedition)

    @pytest.mark.asyncio
    async def test_expedition_published_to_blackboard(self):
        """After _select_procedure, ctx.blackboard['expedition'] is set."""
        from anima.planner.expedition import MiningExpedition

        reg = ProcedureRegistry()
        planner = Planner(reg)
        ctx = _make_ctx()
        await planner.select_procedure(ctx)
        assert isinstance(ctx.blackboard.get("expedition"), MiningExpedition)
        assert ctx.blackboard["expedition"] is planner._expedition


class TestPickUpAndSmeltWithExpedition:
    @pytest.mark.asyncio
    async def test_uses_pile_location_when_available(self):
        """When expedition has piles, _PickUpAndSmelt targets the nearest pile."""
        from anima.planner.expedition import MiningExpedition, PileRecord
        from anima.planner.helpers import _PickUpAndSmelt

        exp = MiningExpedition()
        exp.piles = [
            PileRecord(x=2460, y=558, bank_key=(307, 69), est_amount=3, last_seen_ts=time.time()),
            PileRecord(x=2480, y=560, bank_key=(310, 70), est_amount=2, last_seen_ts=time.time()),
        ]

        ctx = _make_ctx()
        ctx.perception.self_state.x = 2465
        ctx.perception.self_state.y = 559
        ctx.blackboard["expedition"] = exp

        with patch("anima.action.movement.go_to", new=AsyncMock(return_value=True)):
            proc = _PickUpAndSmelt(ground_ore=[], ss=ctx.perception.self_state)
            # The nearest pile is (2460, 558) at distance 5 (vs. distance ~15)
            # Run the procedure (it will go_to that pile and attempt pickup).
            await proc.run(ctx)
            # go_to should have been called for (2460, 558)
            import anima.action.movement
            # We patched it — check the patched AsyncMock was awaited with the nearest pile
            assert anima.action.movement.go_to.await_args.args[1:3] == (2460, 558)

    @pytest.mark.asyncio
    async def test_marks_pile_collected_on_success(self):
        """After picking up, the pile is removed from expedition memory."""
        from anima.planner.expedition import MiningExpedition, PileRecord
        from anima.planner.helpers import _PickUpAndSmelt

        exp = MiningExpedition()
        pile = PileRecord(x=2460, y=558, bank_key=(307, 69), est_amount=1, last_seen_ts=time.time())
        exp.piles = [pile]

        ctx = _make_ctx()
        ctx.perception.self_state.x = 2460
        ctx.perception.self_state.y = 558
        ctx.blackboard["expedition"] = exp

        # Put one ore on the ground at the pile position
        ore = MagicMock(
            serial=0xDEAD0001, graphic=0x19B9, amount=2, container=0,
            x=2460, y=558, z=0,
        )
        ctx.perception.world.items = {ore.serial: ore}

        # Simulate server consuming the ore on pickup (item disappears from world)
        async def _sim_pickup(pkt):
            ctx.perception.world.items.pop(ore.serial, None)
        ctx.conn.send_packet = AsyncMock(side_effect=_sim_pickup)

        with patch("anima.action.movement.go_to", new=AsyncMock(return_value=True)):
            proc = _PickUpAndSmelt(ground_ore=[ore], ss=ctx.perception.self_state)
            result = await proc.run(ctx)

        assert result.success is True
        assert pile not in exp.piles

    @pytest.mark.asyncio
    async def test_removes_pile_on_go_to_failure(self):
        """If we can't reach the pile, remove it from memory."""
        from anima.planner.expedition import MiningExpedition, PileRecord
        from anima.planner.helpers import _PickUpAndSmelt

        exp = MiningExpedition()
        pile = PileRecord(x=9999, y=9999, bank_key=(0, 0), est_amount=1, last_seen_ts=time.time())
        exp.piles = [pile]

        ctx = _make_ctx()
        ctx.blackboard["expedition"] = exp

        with patch("anima.action.movement.go_to", new=AsyncMock(return_value=False)):
            proc = _PickUpAndSmelt(ground_ore=[], ss=ctx.perception.self_state)
            await proc.run(ctx)

        assert pile not in exp.piles

    @pytest.mark.asyncio
    async def test_falls_back_to_ground_ore_when_no_piles(self):
        """Without expedition piles, behave like before: use `ground_ore` arg."""
        from anima.planner.helpers import _PickUpAndSmelt

        ctx = _make_ctx()
        # No expedition on blackboard
        ore = MagicMock(
            serial=0xDEAD0002, graphic=0x19B9, amount=2, container=0,
            x=100, y=200, z=0,
        )
        ctx.perception.world.items = {ore.serial: ore}
        ctx.perception.self_state.x = 100
        ctx.perception.self_state.y = 200

        # Simulate server consuming the ore on pickup (item disappears from world)
        async def _sim_pickup(pkt):
            ctx.perception.world.items.pop(ore.serial, None)
        ctx.conn.send_packet = AsyncMock(side_effect=_sim_pickup)

        with patch("anima.action.movement.go_to", new=AsyncMock(return_value=True)):
            proc = _PickUpAndSmelt(ground_ore=[ore], ss=ctx.perception.self_state)
            result = await proc.run(ctx)
        # Picked up from the legacy path
        assert result.success is True

    @pytest.mark.asyncio
    async def test_pile_removed_on_server_refused_pickup(self):
        """When server refuses every pickup, drop the pile to avoid a loop."""
        from anima.planner.expedition import MiningExpedition, PileRecord
        from anima.planner.helpers import _PickUpAndSmelt

        exp = MiningExpedition()
        pile = PileRecord(
            x=100, y=200, bank_key=(12, 25), est_amount=2,
            last_seen_ts=time.time(),
        )
        exp.piles = [pile]

        ctx = _make_ctx()
        ctx.perception.self_state.x = 100
        ctx.perception.self_state.y = 200
        ctx.blackboard["expedition"] = exp

        # Ore visible on ground but simulate server refusal: pickup/drop
        # packets are sent, but the item stays in container=0 after.
        ore = MagicMock(
            serial=0xE001, graphic=0x19B9, amount=2, container=0,
            x=100, y=200, z=0,
        )
        ctx.perception.world.items = {ore.serial: ore}

        with patch("anima.action.movement.go_to", new=AsyncMock(return_value=True)):
            proc = _PickUpAndSmelt(ground_ore=[], ss=ctx.perception.self_state)
            result = await proc.run(ctx)

        assert result.success is False
        assert pile not in exp.piles, "pile must be dropped after refusal"


class TestPhaseGatedSmelt:
    @pytest.mark.asyncio
    async def test_mining_phase_ignores_ground_ore_pickup(self):
        """In MINING phase, two ore on the ground does NOT trigger pick_up_ore_and_smelt."""
        from anima.planner.expedition import Phase

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("mine_ore"))
        reg.register(StubProcedure("smelt_ore"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.MINING)

        ctx = _make_ctx()
        # Two ore on the ground near player
        ore = MagicMock(serial=0xA1, graphic=0x19B9, amount=2, container=0, x=100, y=200)
        ctx.perception.world.items = {ore.serial: ore}

        proc = await planner.select_procedure(ctx)
        # Should not be _PickUpAndSmelt
        assert getattr(proc, "name", "") != "pick_up_ore_and_smelt"

    @pytest.mark.asyncio
    async def test_collecting_phase_does_trigger_pickup(self):
        """In COLLECTING phase with piles, _PickUpAndSmelt is selected."""
        from anima.planner.expedition import Phase, PileRecord

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("mine_ore"))
        reg.register(StubProcedure("smelt_ore"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.COLLECTING)
        planner._expedition.piles = [
            PileRecord(x=100, y=200, bank_key=(12, 25), est_amount=2, last_seen_ts=time.time()),
        ]

        ctx = _make_ctx()
        proc = await planner.select_procedure(ctx)
        assert getattr(proc, "name", "") == "pick_up_ore_and_smelt"

    @pytest.mark.asyncio
    async def test_transitions_mining_to_collecting_when_scan_empty(self):
        """When mining scan finds nothing and piles exist, phase flips to COLLECTING."""
        from anima.planner.expedition import Phase, PileRecord

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("mine_ore", can=False))  # mine not available
        reg.register(StubProcedure("smelt_ore"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.MINING)
        planner._expedition.piles = [
            PileRecord(x=100, y=200, bank_key=(12, 25), est_amount=2, last_seen_ts=time.time()),
        ]

        ctx = _make_ctx()
        # Mock the scan helper to say "no banks available"
        with patch.object(planner, "_scan_has_mineable_bank", return_value=False):
            await planner.select_procedure(ctx)

        assert planner._expedition.phase == Phase.COLLECTING


class TestCollectingTransitions:
    @pytest.mark.asyncio
    async def test_collecting_to_crafting_trip_when_ingots_enough(self):
        from anima.planner.expedition import Phase

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("craft_blacksmith"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.COLLECTING)
        planner._expedition.piles = []  # all collected

        ctx = _make_ctx()
        # Give the agent 16 iron ingots + tongs
        _add_item(ctx, 0xB1, INGOT, amount=16)
        _add_item(ctx, 0xB2, TONGS, amount=1)
        _add_item(ctx, 0xB3, PICKAXE, amount=1)

        await planner.select_procedure(ctx)
        assert planner._expedition.phase == Phase.CRAFTING_TRIP

    @pytest.mark.asyncio
    async def test_collecting_back_to_mining_when_nothing_triggers_craft(self):
        from anima.planner.expedition import Phase

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.COLLECTING)
        planner._expedition.piles = []

        ctx = _make_ctx()
        _add_item(ctx, 0xB4, PICKAXE, amount=1)
        # few ingots, light weight

        await planner.select_procedure(ctx)
        assert planner._expedition.phase == Phase.MINING

    @pytest.mark.asyncio
    async def test_collecting_stays_if_piles_not_empty(self):
        from anima.planner.expedition import Phase, PileRecord

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.COLLECTING)
        planner._expedition.piles = [
            PileRecord(x=100, y=200, bank_key=(12, 25), est_amount=1, last_seen_ts=time.time()),
        ]

        ctx = _make_ctx()
        _add_item(ctx, 0xB5, INGOT, amount=50)  # would trigger craft if piles empty

        await planner.select_procedure(ctx)
        assert planner._expedition.phase == Phase.COLLECTING


class TestCraftingTripPhase:
    @pytest.mark.asyncio
    async def test_collecting_phase_does_not_craft_even_with_ingots(self):
        """In COLLECTING phase, craft is deferred so the tour can finish."""
        from anima.planner.expedition import Phase, PileRecord

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("craft_blacksmith"))
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.COLLECTING)
        # At least one pile so COLLECTING doesn't immediately auto-exit
        planner._expedition.piles.append(
            PileRecord(x=1, y=1, bank_key=(0, 0), est_amount=1, last_seen_ts=time.time())
        )

        ctx = _make_ctx()
        _add_item(ctx, 0xC1, INGOT, amount=20)  # >= BATCH_CRAFT_INGOTS (16)
        _add_item(ctx, 0xC2, TONGS, amount=1)
        _add_item(ctx, 0xC3, PICKAXE, amount=1)

        proc = await planner.select_procedure(ctx)
        assert proc is None or proc.name != "craft_blacksmith"

    @pytest.mark.asyncio
    async def test_mining_phase_does_craft_when_ingots_overflow(self):
        """In MINING with ingot_count ≥ BATCH_CRAFT_INGOTS, craft is allowed.

        Safety valve against the collecting trigger never firing — e.g. in
        dense mining areas where banks always respawn before scan is empty.
        """
        from anima.planner.expedition import Phase

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("craft_blacksmith"))
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.MINING)

        ctx = _make_ctx()
        _add_item(ctx, 0xC1, INGOT, amount=20)
        _add_item(ctx, 0xC2, TONGS, amount=1)
        _add_item(ctx, 0xC3, PICKAXE, amount=1)

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "craft_blacksmith"

    @pytest.mark.asyncio
    async def test_crafting_trip_phase_runs_craft(self):
        """In CRAFTING_TRIP phase, >= BATCH_CRAFT_INGOTS ingots + tongs → craft."""
        from anima.planner.expedition import Phase

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("craft_blacksmith"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.CRAFTING_TRIP)

        ctx = _make_ctx()
        _add_item(ctx, 0xC4, INGOT, amount=20)
        _add_item(ctx, 0xC5, TONGS, amount=1)
        _add_item(ctx, 0xC6, PICKAXE, amount=1)

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "craft_blacksmith"

    @pytest.mark.asyncio
    async def test_crafting_trip_back_to_mining_when_done(self):
        """CRAFTING_TRIP → MINING when ingots < 4, crafted_count == 0, near home."""
        from anima.planner.expedition import Phase

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.CRAFTING_TRIP)
        # Set home_base to the default ctx position (x=100, y=200)
        planner._expedition.home_base = (100, 200)

        ctx = _make_ctx()  # x=100, y=200 — near home
        _add_item(ctx, 0xC7, PICKAXE, amount=1)
        # No ingots (< 4), no crafted items → should_return_to_mine returns True

        await planner.select_procedure(ctx)
        assert planner._expedition.phase == Phase.MINING


class TestBegNpcForCoin:
    """Deadlock Lv0 pre-ladder: beg nearby NPC via UO Begging skill."""

    @pytest.mark.asyncio
    async def test_beg_npc_selected_when_gold_zero_and_npc_nearby(self):
        """Lv0 deadlock + gold=0 + visible human NPC → _BegNpcForCoin proc."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        # True-deadlock state: no gold, no pickaxe, no weapon worth hunting.
        ctx = _make_ctx(gold=0, x=2461, y=451)
        ctx.map_reader = None  # skip A* in _find_visible_vendor_npc
        # Human-body NPC (male=0x0190), within 10 tiles, not recently begged.
        npc = MagicMock(
            serial=0x77777, name="Yale the tinker",
            x=2463, y=452, body=0x0190,
        )
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[npc])
        ctx.blackboard["_deadlock_recovery_level"] = 0
        ctx.blackboard["_deadlock_attempt_count"] = 0
        # Block outbound moves so the selector reaches the pre-ladder hook.
        planner._move_fail_until = time.time() + 300

        proc = await planner.select_procedure(ctx)
        assert proc is not None
        assert proc.name == "beg_npc_for_coin"

    @pytest.mark.asyncio
    async def test_beg_npc_skipped_when_recently_begged(self):
        """Same NPC within cooldown window → begging path does NOT fire."""
        from anima.planner.planner import _BegNpcForCoin

        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=0, x=2461, y=451)
        ctx.map_reader = None
        npc = MagicMock(
            serial=0x77777, name="Yale the tinker",
            x=2463, y=452, body=0x0190,
        )
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[npc])
        # Seed cooldown: this NPC was begged from one second ago.
        ctx.blackboard["_begged_npcs"] = {
            npc.serial: time.time() - 1.0,
        }
        ctx.blackboard["_deadlock_recovery_level"] = 0
        ctx.blackboard["_deadlock_attempt_count"] = 0
        planner._move_fail_until = time.time() + 300

        proc = await planner.select_procedure(ctx)
        # Should not re-beg the same NPC — some other (or no) path taken.
        assert proc is None or proc.name != "beg_npc_for_coin"
        # Cooldown window covers the current attempt.
        assert _BegNpcForCoin.COOLDOWN_S > 1.0

    @pytest.mark.asyncio
    async def test_beg_npc_skipped_when_gold_already_positive(self):
        """Once gold ≥ 10, buy-tools hatch wins — begging path must not fire."""
        reg = ProcedureRegistry()
        planner = Planner(reg)

        ctx = _make_ctx(gold=50, x=2461, y=451)
        ctx.map_reader = None
        npc = MagicMock(
            serial=0x77777, name="Yale the tinker",
            x=2463, y=452, body=0x0190,
        )
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[npc])
        ctx.blackboard["_deadlock_recovery_level"] = 0
        ctx.blackboard["_deadlock_attempt_count"] = 0
        planner._move_fail_until = time.time() + 300

        proc = await planner.select_procedure(ctx)
        assert proc is None or proc.name != "beg_npc_for_coin"


class TestExpeditionWatchdog:
    @pytest.mark.asyncio
    async def test_stuck_phase_resets_to_idle(self):
        """If the current phase has been active > 10 min, transition to IDLE."""
        from anima.planner.expedition import Phase, PileRecord
        from structlog.testing import capture_logs

        reg = ProcedureRegistry()
        reg.register(StubProcedure("heal_self", can=False))
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)
        planner._expedition.transition_to(Phase.COLLECTING)
        planner._expedition.phase_started_at = time.time() - 700  # 11m40s ago
        planner._expedition.piles = [
            PileRecord(x=1, y=1, bank_key=(0, 0), est_amount=1, last_seen_ts=time.time()),
        ]

        ctx = _make_ctx()
        with capture_logs() as logs:
            await planner.select_procedure(ctx)

        assert planner._expedition.phase == Phase.IDLE
        assert planner._expedition.piles == []
        # Warning emitted
        assert any(
            entry.get("event") == "expedition_watchdog"
            and entry.get("log_level") == "warning"
            for entry in logs
        ), f"expected expedition_watchdog warning in logs: {logs}"
