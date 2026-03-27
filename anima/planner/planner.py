"""Rule-based planner — selects next procedure based on priority rules.

Priority order:
  1. Survival (HP < 30% → flee/heal)
  2. Social (pending speech → respond)
  3. Weight management (> 85% → smelt or bank)
  4. Tool management (no tools → make or buy)
  5. Primary activity (mine/craft based on character)
  6. Secondary activities (sell, bank)
  7. LLM strategic decision (fallback)

Continuation hints prevent thrashing between procedures.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from anima.procedures.base import FailureReason, ProcedureRegistry, ProcedureResult

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

# Minimum delay between planner loops to prevent spin on rapid failures
MIN_LOOP_DELAY = 0.2


class Planner:
    """Selects and runs procedures based on priority rules."""

    def __init__(self, registry: ProcedureRegistry) -> None:
        self.registry = registry
        self.continuation_hint: str | None = None
        self._running = False
        self._last_trade_time: float = 0.0
        self._move_fail_until: float = 0.0  # cooldown after move-to failure
        self._failed_destinations: dict[tuple[int, int], float] = {}  # (x,y) → time
        self._last_backpack_request: float = 0.0  # cooldown for re-requesting equipment

    def stop(self) -> None:
        self._running = False

    async def run(self, ctx: AgentContext) -> None:
        """Main planner loop. Runs until connection drops."""
        self._running = True
        logger.info("planner_started")

        while self._running and ctx.conn.connected:
            try:
                result = await self.tick(ctx)
                if result:
                    self.continuation_hint = result.next_suggestion
                    # If move_to failed, cooldown before retry
                    if not result.success and hasattr(result, 'message') and 'Could not reach' in (result.message or ''):
                        import time
                        self._move_fail_until = time.time() + 30.0
                        logger.info("planner_move_cooldown", seconds=30)
                else:
                    self.continuation_hint = None
            except Exception as e:
                logger.error("planner_tick_error", error=str(e))

            await asyncio.sleep(MIN_LOOP_DELAY)

        logger.info("planner_stopped")

    async def tick(self, ctx: AgentContext) -> ProcedureResult | None:
        """One planner cycle: select procedure → run it → return result."""
        proc = await self.select_procedure(ctx)
        if proc is None:
            return None

        logger.info("planner_selected", procedure=proc.name)

        # Publish activity to bus for TUI display
        if ctx.bus:
            ctx.bus.publish("action.start", {
                "message": f"▶ {proc.name}",
                "importance": 1,
            })

        result = await proc.run(ctx)

        # Track failed move destinations to prevent retry loops
        if result and not result.success and isinstance(proc, _MoveToProcedure):
            import time
            self._failed_destinations[(proc._x, proc._y)] = time.time()

        # Log and publish result
        if result:
            icon = "✓" if result.success else "✗"
            logger.info(
                "planner_result",
                procedure=proc.name,
                success=result.success,
                reason=result.reason.value if result.reason else None,
                message=result.message[:80] if result.message else "",
                hint=result.next_suggestion,
            )
            if ctx.bus:
                ctx.bus.publish("action.end", {
                    "message": f"{icon} {proc.name}: {result.message}",
                    "importance": 2 if result.success else 1,
                })

        return result

    async def select_procedure(self, ctx: AgentContext):
        """Select the highest-priority procedure based on gameplay loop.

        Full loop: mine → smelt → sell → bank → buy tools → return to mine

        Priority:
          1. Survival (HP low)
          2. Overweight → smelt ore (if forge nearby) or go to forge
          3. Has ore on ground → smelt
          4. Has ingots → sell to vendor (if nearby) or go to vendor
          5. Has gold > threshold → bank deposit (if nearby) or go to bank
          6. No pickaxe → go buy one
          7. Has pickaxe → mine ore
          8. Nothing to do → move to mine
        """
        import time
        from anima.actions.inventory import find_in_backpack, count_items
        from anima.skills.gathering.mine import PICKAXE_GRAPHICS, ORE_GRAPHICS
        from anima.skills.crafting.smelt import INGOT_GRAPHICS

        ss = ctx.perception.self_state
        backpack = ss.equipment.get(0x15)

        if not backpack:
            # Re-request equipment periodically (every 15s) to recover
            now = time.time()
            if now - self._last_backpack_request > 15.0:
                self._last_backpack_request = now
                logger.info("planner_requesting_backpack")
                from anima.client.packets import build_double_click
                await ctx.conn.send_packet(build_double_click(ss.serial))

            # Still allow survival and movement without backpack
            if ss.hits_max > 0 and ss.hits < ss.hits_max * 0.3:
                proc = self.registry.get("heal_self")
                if proc and await proc.can_start(ctx):
                    return proc

            if time.time() > self._move_fail_until:
                move_proc = await self._try_move_to_activity(ctx)
                if move_proc:
                    return move_proc

            logger.debug("planner_no_backpack")
            return None

        from anima.skills.crafting.tinker import TINKER_TOOLS_GRAPHICS

        SHOVEL_GRAPHICS = {0x0F39}

        # --- Inventory snapshot ---
        has_mining_tool = bool(find_in_backpack(ctx, PICKAXE_GRAPHICS | SHOVEL_GRAPHICS))
        has_tinker_tools = bool(find_in_backpack(ctx, TINKER_TOOLS_GRAPHICS))
        ore_count = count_items(ctx, ORE_GRAPHICS)
        ingot_count = count_items(ctx, INGOT_GRAPHICS)

        from anima.procedures.craft_blacksmith import CRAFTED_ITEM_GRAPHICS
        crafted_count = sum(
            1 for it in ctx.perception.world.items.values()
            if it.container == backpack and it.graphic in CRAFTED_ITEM_GRAPHICS
        )

        # Periodic state snapshot (every 30s)
        now = time.time()
        if now - getattr(self, '_last_state_log', 0) > 30:
            self._last_state_log = now
            logger.info(
                "planner_state",
                pos=f"({ss.x},{ss.y},z={ss.z})",
                gold=ss.gold,
                weight=f"{ss.weight}/{ss.weight_max}",
                tool=has_mining_tool,
                tinker=has_tinker_tools,
                ore=ore_count,
                ingot=ingot_count,
                crafted=crafted_count,
                gave_up_craft=bool(ctx.blackboard.get("_make_tools_gave_up")),
            )

        # --- Priority 1: Survival ---
        if ss.hits_max > 0 and ss.hits < ss.hits_max * 0.3:
            proc = self.registry.get("heal_self")
            if proc and await proc.can_start(ctx):
                return proc

        # --- Priority 2: Overweight → smelt ---
        if ss.weight_max > 0 and ss.weight > ss.weight_max * 0.85:
            proc = self.registry.get("smelt_ore")
            if proc and await proc.can_start(ctx):
                return proc
            # No forge nearby — go to forge
            return await self._move_to_location(ctx, "forge", "blacksmith")

        # --- Priority 3: Has ore → smelt ---
        if ore_count > 0:
            proc = self.registry.get("smelt_ore")
            if proc and await proc.can_start(ctx):
                return proc
            # Ore in backpack but no forge nearby — go to forge
            return await self._move_to_location(ctx, "forge", "blacksmith")

        # --- Priority 3b: Ore on ground nearby → pick up then go smelt ---
        ground_ore = self._find_ground_ore(ctx, ss)
        if ground_ore:
            return _PickUpAndSmelt(ground_ore, ss)

        # --- Priority 4: No mining tools → get them ---
        if not has_mining_tool:
            # 4a: Has ore → smelt first (need ingots to craft or sell for gold)
            if ore_count > 0:
                proc = self.registry.get("smelt_ore")
                if proc and await proc.can_start(ctx):
                    return proc
                return await self._move_to_location(ctx, "forge", "blacksmith")

            # 4b: Has tinker tools + ingots → craft shovel/pickaxe
            #     Skip if crafting already gave up (buy instead)
            if (has_tinker_tools and ingot_count >= 4
                    and not ctx.blackboard.get("_make_tools_gave_up")):
                proc = self.registry.get("make_tools")
                if proc and await proc.can_start(ctx):
                    return proc

            # 4c: Has ingots but no tinker tools → sell ingots for gold
            if ingot_count > 0:
                proc = self.registry.get("sell_to_vendor")
                if proc and await proc.can_start(ctx):
                    return proc
                return await self._move_to_location(ctx, "blacksmith", "weaponsmith", "provisioner")

            # 4d: Has gold → buy tools
            if ss.gold >= 20:
                proc = self.registry.get("buy_from_vendor")
                if proc and await proc.can_start(ctx):
                    return proc
                return await self._move_to_location(ctx, "tinker", "provisioner")

            # 4e: No gold, no ore, no ingots — truly stuck
            logger.warning("planner_stuck_no_resources")
            return None

        # --- Priority 5: Has ingots → craft into weapons/armor ---
        if ingot_count >= 8:
            proc = self.registry.get("craft_blacksmith")
            if proc and await proc.can_start(ctx):
                return proc
            # Need forge/anvil — go to blacksmith
            move = await self._move_to_location(ctx, "forge", "blacksmith")
            if move:
                return move

        # --- Priority 5b: Has crafted items → sell ---
        from anima.procedures.craft_blacksmith import CRAFTED_ITEM_GRAPHICS
        backpack = ss.equipment.get(0x15)
        crafted_count = sum(
            1 for it in ctx.perception.world.items.values()
            if it.container == backpack and it.graphic in CRAFTED_ITEM_GRAPHICS
        ) if backpack else 0
        if crafted_count > 0:
            proc = self.registry.get("sell_to_vendor")
            if proc and await proc.can_start(ctx):
                return proc
            move = await self._move_to_location(ctx, "weaponsmith", "blacksmith", "arms")
            if move:
                return move

        # --- Priority 5c: Has ingots but can't craft → sell raw ingots ---
        if ingot_count >= 10:
            proc = self.registry.get("sell_to_vendor")
            if proc and await proc.can_start(ctx):
                return proc
            move = await self._move_to_location(ctx, "blacksmith", "weaponsmith")
            if move:
                return move

        # --- Priority 6: Gold > 200 → bank ---
        if ss.gold > 200:
            proc = self.registry.get("bank_deposit")
            if proc and await proc.can_start(ctx):
                return proc
            return await self._move_to_location(ctx, "bank")

        # --- Priority 7: Has mining tool → mine ---
        proc = self.registry.get("mine_ore")
        if proc and await proc.can_start(ctx):
            return proc

        # --- Priority 8: Continuation hint ---
        if self.continuation_hint:
            proc = self.registry.get(self.continuation_hint)
            if proc and await proc.can_start(ctx):
                return proc
            self.continuation_hint = None

        # --- Priority 9: Move to mine ---
        if time.time() > self._move_fail_until:
            move_proc = await self._try_move_to_activity(ctx)
            if move_proc:
                return move_proc

        logger.debug("planner_no_procedure_available")
        return None

    def _find_ground_ore(self, ctx: AgentContext, ss) -> list:
        """Find ore items on the ground near the player (excluding junk)."""
        from anima.skills.gathering.mine import ORE_GRAPHICS
        junk = ctx.blackboard.get("_junk_ore_serials", set())
        result = []
        for it in ctx.perception.world.items.values():
            if (it.container == 0
                    and it.graphic in ORE_GRAPHICS
                    and it.serial not in junk
                    and max(abs(it.x - ss.x), abs(it.y - ss.y)) <= 3):
                result.append(it)
        return result

    def _is_destination_failed(self, x: int, y: int) -> bool:
        """Check if a destination recently failed to be reached (5-min cooldown)."""
        import time
        ts = self._failed_destinations.get((x, y))
        if ts is None:
            return False
        if time.time() - ts < 300.0:
            return True
        del self._failed_destinations[(x, y)]
        return False

    async def _move_to_location(self, ctx: AgentContext, *keywords: str, max_dist: int = 300):
        """Find nearest location matching any keyword and move there.

        max_dist caps the search radius to avoid cross-city navigation attempts
        (e.g., trying Britain vendors when in Minoc).
        """
        import time as _time
        from anima.world_knowledge import ALL_LOCATIONS

        ss = ctx.perception.self_state

        # Mark locations we're already standing at as temporarily failed.
        # This prevents ping-pong: arrive at vendor location → can't sell →
        # walk to next vendor → can't sell → walk back to first one → repeat.
        for loc in ALL_LOCATIONS:
            name_lower = loc.name.lower()
            if any(kw in name_lower for kw in keywords):
                dist = max(abs(loc.x - ss.x), abs(loc.y - ss.y))
                if dist <= 3 and (loc.x, loc.y) not in self._failed_destinations:
                    self._failed_destinations[(loc.x, loc.y)] = _time.time()

        best = None
        best_dist = 999999
        for loc in ALL_LOCATIONS:
            name_lower = loc.name.lower()
            if any(kw in name_lower for kw in keywords):
                dist = max(abs(loc.x - ss.x), abs(loc.y - ss.y))
                if dist > max_dist:
                    continue  # skip locations in other cities
                if dist > 3 and dist < best_dist:
                    if self._is_destination_failed(loc.x, loc.y):
                        continue
                    best_dist = dist
                    best = loc

        if best:
            logger.info("planner_move_to", target=best.name, dist=best_dist)
            if ctx.bus:
                ctx.bus.publish("movement.start", {
                    "message": f"→ Moving to {best.name} (dist {best_dist})",
                    "importance": 2,
                })
            return _MoveToProcedure(best.name, best.x, best.y)
        return None

    async def _try_move_to_activity(self, ctx: AgentContext):
        """If no procedure can start, walk toward primary activity location.

        Uses waypoint routing: if the target is far, finds intermediate
        waypoints along the way to avoid getting stuck on building walls.
        """
        import re
        from anima.world_knowledge import ALL_LOCATIONS

        ss = ctx.perception.self_state

        # Find nearest activity location using word boundaries so that
        # "mine" matches "East Mine" but not "Miners Guild".
        _ACTIVITY_RE = re.compile(r'\b(mine|mining|mountain|forest)\b', re.IGNORECASE)
        max_activity_dist = 300  # prevent cross-city routing
        mine_loc = None
        best_dist = 999999
        for loc in ALL_LOCATIONS:
            if _ACTIVITY_RE.search(loc.name):
                if self._is_destination_failed(loc.x, loc.y):
                    continue
                dist = max(abs(loc.x - ss.x), abs(loc.y - ss.y))
                if dist <= 5:
                    continue  # Already here — skip to find next location
                if dist > max_activity_dist:
                    continue  # Skip locations in other cities
                if dist < best_dist:
                    best_dist = dist
                    mine_loc = loc

        if not mine_loc:
            return None

        # If far away, find intermediate waypoints
        # Pick the waypoint closest to the line between current pos and target
        target = mine_loc
        if best_dist > 30:
            waypoint = _find_waypoint_toward(ss.x, ss.y, target.x, target.y, ALL_LOCATIONS)
            if waypoint and not self._is_destination_failed(waypoint.x, waypoint.y):
                logger.info(
                    "planner_waypoint_routing",
                    via=waypoint.name,
                    pos=f"({waypoint.x},{waypoint.y})",
                    final_target=target.name,
                )
                return _MoveToProcedure(waypoint.name, waypoint.x, waypoint.y)

        logger.info(
            "planner_moving_to_activity",
            target=target.name,
            pos=f"({target.x},{target.y})",
            dist=best_dist,
        )
        if ctx.bus:
            ctx.bus.publish("movement.start", {
                "message": f"⛏ Heading to {target.name} (dist {best_dist})",
                "importance": 2,
            })
        return _MoveToProcedure(target.name, target.x, target.y)


def _find_waypoint_toward(sx, sy, tx, ty, locations) -> object | None:
    """Find the best intermediate waypoint between (sx,sy) and (tx,ty).

    Picks a waypoint that:
    1. Is closer to the target than we are
    2. Is closer to us than the target is
    3. Is roughly on the path (not a big detour)
    """
    current_dist = max(abs(tx - sx), abs(ty - sy))
    best = None
    best_score = float("inf")

    for loc in locations:
        loc_to_target = max(abs(tx - loc.x), abs(ty - loc.y))
        loc_to_us = max(abs(sx - loc.x), abs(sy - loc.y))

        # Must be closer to target than we are
        if loc_to_target >= current_dist:
            continue
        # Must be reachable (not too far from us)
        if loc_to_us >= current_dist:
            continue
        # Must be closer to us than the target
        if loc_to_us < 5:
            continue  # already here

        # Score: lower is better — prefer waypoints that progress toward target
        score = loc_to_us + loc_to_target
        if score < best_score:
            best_score = score
            best = loc

    return best


class _PickUpAndSmelt:
    """Pick up ground ore into backpack, then move to forge to smelt."""

    def __init__(self, ground_ore: list, ss) -> None:
        self.name = "pick_up_ore_and_smelt"
        self.description = "Pick up ore from ground, go to forge"
        self._ore_items = ground_ore
        self._player_pos = (ss.x, ss.y)

    async def can_start(self, ctx) -> bool:
        return True

    async def run(self, ctx) -> ProcedureResult:
        import asyncio
        from anima.client.packets import build_drop_item, build_pick_up

        ss = ctx.perception.self_state
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return ProcedureResult(success=False, reason=FailureReason.MISSING_RESOURCE, message="no backpack")

        # Pick up ore from ground into backpack (up to weight limit)
        picked = 0
        for ore in self._ore_items:
            # Check weight — stop if getting heavy (leave 50 stone buffer)
            if ss.weight_max > 0 and ss.weight > ss.weight_max - 50:
                break
            ore_item = ctx.perception.world.items.get(ore.serial)
            if not ore_item or ore_item.container != 0:
                continue
            await ctx.conn.send_packet(build_pick_up(ore_item.serial, ore_item.amount))
            await asyncio.sleep(0.3)
            await ctx.conn.send_packet(
                build_drop_item(ore_item.serial, container=backpack)
            )
            await asyncio.sleep(0.3)
            picked += 1
            logger.info("picked_up_ore", serial=f"0x{ore_item.serial:08X}", amount=ore_item.amount)

        if picked == 0:
            return ProcedureResult(success=False, reason=FailureReason.MISSING_RESOURCE, message="no ore to pick up")

        # Now move to forge
        from anima.world_knowledge import ALL_LOCATIONS
        forge = None
        best_dist = 999999
        for loc in ALL_LOCATIONS:
            if "forge" in loc.name.lower() or "blacksmith" in loc.name.lower():
                dist = max(abs(loc.x - ss.x), abs(loc.y - ss.y))
                if dist < best_dist:
                    best_dist = dist
                    forge = loc

        if forge and best_dist > 3:
            from anima.action.movement import go_to
            logger.info("moving_to_forge", target=forge.name, dist=best_dist)
            await go_to(ctx, forge.x, forge.y)

        return ProcedureResult(
            success=True,
            message=f"Picked up {picked} ore stacks, heading to forge",
            next_suggestion="smelt_ore",
        )


class _MoveToProcedure:
    """Temporary one-shot procedure to walk to a destination."""

    def __init__(self, name: str, x: int, y: int) -> None:
        self.name = f"move_to_{name}"
        self.description = f"Walk to {name}"
        self._x = x
        self._y = y

    async def can_start(self, ctx) -> bool:
        return True

    async def run(self, ctx) -> ProcedureResult:
        from anima.action.movement import go_to

        arrived = await go_to(
            ctx, self._x, self._y,
            interrupt_check=lambda: (
                ctx.perception.self_state.hits_max > 0
                and ctx.perception.self_state.hits < ctx.perception.self_state.hits_max * 0.3
            ),
        )
        if arrived:
            return ProcedureResult(success=True, message=f"Arrived at {self.description}")
        else:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"Could not reach {self.description}",
            )
