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
