"""Helper procedures instantiated directly by the planner.

These are not registered with the ProcedureRegistry — the planner
constructs them inline when it needs ad-hoc behavior (move to a
location, pick up and smelt a specific ore pile, scavenge for deadlock
recovery). Their `run()` methods are the interface the planner calls.

Extracted from planner.py in Task 4.1 to shrink that file and make
each helper independently testable.
"""

from __future__ import annotations

import asyncio

import structlog

from anima.procedures.base import FailureReason, ProcedureResult

logger = structlog.get_logger()


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
        from anima.action.movement import go_to
        from anima.client.packets import build_drop_item, build_pick_up

        ss = ctx.perception.self_state
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no backpack",
            )

        expedition = ctx.blackboard.get("expedition")

        # --- Path A: expedition with remembered piles ---
        if expedition is not None and expedition.piles:
            # Nearest pile first
            piles_sorted = sorted(
                expedition.piles,
                key=lambda p: max(abs(p.x - ss.x), abs(p.y - ss.y)),
            )
            target_pile = piles_sorted[0]
            pile_dist = max(abs(target_pile.x - ss.x), abs(target_pile.y - ss.y))
            if pile_dist > 2:
                arrived = await go_to(ctx, target_pile.x, target_pile.y)
                if not arrived:
                    logger.info(
                        "pile_unreachable_removed",
                        pos=f"({target_pile.x},{target_pile.y})",
                    )
                    expedition.mark_pile_collected(target_pile)
                    return ProcedureResult(
                        success=False,
                        reason=FailureReason.BLOCKED,
                        message=f"could not reach pile ({target_pile.x},{target_pile.y})",
                    )

            # Find ore within 2 tiles of current position
            nearby_ore = [
                it for it in ctx.perception.world.items.values()
                if it.graphic in (0x19B7, 0x19B8, 0x19B9, 0x19BA)
                and it.container == 0
                and max(abs(it.x - ss.x), abs(it.y - ss.y)) <= 2
            ]
            if not nearby_ore:
                logger.info(
                    "pile_empty_removed",
                    pos=f"({target_pile.x},{target_pile.y})",
                )
                expedition.mark_pile_collected(target_pile)
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.MISSING_RESOURCE,
                    message="pile empty at target",
                )

            picked = await self._pickup_into_backpack(ctx, nearby_ore, backpack)
            if picked > 0:
                expedition.mark_pile_collected(target_pile)
            else:
                # Server refused every pickup attempt despite visible ore
                # (LOS / z-level / anti-cheat). Drop the pile to avoid a
                # loop; the ore stays on the ground for the Path B fallback.
                logger.warning(
                    "pile_pickup_refused_dropped",
                    pos=f"({target_pile.x},{target_pile.y})",
                    nearby_ore=len(nearby_ore),
                )
                expedition.mark_pile_collected(target_pile)
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.BLOCKED,
                    message="pile pickup refused — removed from memory",
                )

            await self._walk_to_forge(ctx)
            return ProcedureResult(
                success=True,
                message=f"Picked up {picked} ore at pile ({target_pile.x},{target_pile.y}), heading to forge",
                next_suggestion="smelt_ore",
            )

        # --- Path B: legacy ground-ore fallback (no expedition / no piles) ---
        fail_counts: dict[int, int] = ctx.blackboard.setdefault(
            "_ore_pickup_fails", {}
        )
        junk: set[int] = ctx.blackboard.setdefault("_junk_ore_serials", set())

        picked = 0
        hard_failures: list[int] = []
        for ore in self._ore_items:
            if ss.weight_max > 0 and ss.weight > ss.weight_max - 50:
                break
            ore_item = ctx.perception.world.items.get(ore.serial)
            if not ore_item or ore_item.container != 0:
                continue
            dist = max(abs(ore_item.x - ss.x), abs(ore_item.y - ss.y))
            if dist > 2:
                logger.info("ore_too_far", serial=f"0x{ore_item.serial:08X}", dist=dist)
                continue
            await ctx.conn.send_packet(build_pick_up(ore_item.serial, ore_item.amount))
            await asyncio.sleep(0.3)
            await ctx.conn.send_packet(
                build_drop_item(ore_item.serial, container=backpack)
            )
            await asyncio.sleep(0.5)
            ore_check = ctx.perception.world.items.get(ore_item.serial)
            if ore_check and ore_check.container == backpack:
                picked += 1
                fail_counts.pop(ore_item.serial, None)
                breaker = ctx.blackboard.get("_ore_pickup_breaker")
                if breaker is not None:
                    breaker.record_success(ore_item.serial)
                logger.info("picked_up_ore", serial=f"0x{ore_item.serial:08X}", amount=ore_item.amount)
            elif ore_check and ore_check.container == 0:
                breaker = ctx.blackboard.get("_ore_pickup_breaker")
                if breaker is not None:
                    breaker.record_failure(ore_item.serial)
                    opened = breaker.is_open(ore_item.serial)
                    fails = breaker.failure_count(ore_item.serial)
                else:
                    fails = fail_counts.get(ore_item.serial, 0) + 1
                    fail_counts[ore_item.serial] = fails
                    opened = fails >= 2
                logger.warning(
                    "ore_pickup_failed",
                    serial=f"0x{ore_item.serial:08X}",
                    reason="still on ground after pick_up",
                    fails=fails,
                )
                if opened:
                    junk.add(ore_item.serial)
                    hard_failures.append(ore_item.serial)
                    if breaker is not None:
                        breaker.reset(ore_item.serial)
                    else:
                        fail_counts.pop(ore_item.serial, None)
                    logger.info(
                        "ore_marked_junk",
                        serial=f"0x{ore_item.serial:08X}",
                        reason="repeated pickup failure",
                    )
            else:
                picked += 1
                fail_counts.pop(ore_item.serial, None)
                breaker = ctx.blackboard.get("_ore_pickup_breaker")
                if breaker is not None:
                    breaker.record_success(ore_item.serial)
                logger.info(
                    "picked_up_ore",
                    serial=f"0x{ore_item.serial:08X}",
                    amount=ore_item.amount,
                    note="item merged or removed",
                )

        if picked == 0:
            if hard_failures:
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.BLOCKED,
                    message=f"pickup refused by server, marked {len(hard_failures)} ore as junk",
                )
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no ore to pick up",
            )

        await self._walk_to_forge(ctx)
        return ProcedureResult(
            success=True,
            message=f"Picked up {picked} ore stacks, heading to forge",
            next_suggestion="smelt_ore",
        )

    async def _pickup_into_backpack(self, ctx, ore_items, backpack) -> int:
        """Pick each ore in the list into the backpack, respecting weight limit."""
        from anima.client.packets import build_drop_item, build_pick_up

        ss = ctx.perception.self_state
        picked = 0
        for ore_item in ore_items:
            if ss.weight_max > 0 and ss.weight > ss.weight_max - 50:
                break
            current = ctx.perception.world.items.get(ore_item.serial)
            if not current or current.container != 0:
                continue
            await ctx.conn.send_packet(build_pick_up(ore_item.serial, ore_item.amount))
            await asyncio.sleep(0.3)
            await ctx.conn.send_packet(
                build_drop_item(ore_item.serial, container=backpack)
            )
            await asyncio.sleep(0.5)
            verify = ctx.perception.world.items.get(ore_item.serial)
            if verify and verify.container == backpack:
                picked += 1
                logger.info(
                    "picked_up_ore",
                    serial=f"0x{ore_item.serial:08X}",
                    amount=ore_item.amount,
                )
            elif verify is None:
                picked += 1
                logger.info(
                    "picked_up_ore",
                    serial=f"0x{ore_item.serial:08X}",
                    amount=ore_item.amount,
                    note="item merged or removed",
                )
        return picked

    async def _walk_to_forge(self, ctx) -> None:
        """Best-effort walk to the nearest forge/blacksmith location."""
        from anima.action.movement import go_to
        from anima.world_knowledge import ALL_LOCATIONS

        ss = ctx.perception.self_state
        forge = None
        best_dist = 999_999
        for loc in ALL_LOCATIONS:
            if "forge" in loc.name.lower() or "blacksmith" in loc.name.lower():
                d = max(abs(loc.x - ss.x), abs(loc.y - ss.y))
                if d < best_dist:
                    best_dist = d
                    forge = loc
        if forge and best_dist > 3:
            logger.info("moving_to_forge", target=forge.name, dist=best_dist)
            await go_to(ctx, forge.nav_x, forge.nav_y)


class _ScavengeGroundItems:
    """Deadlock recovery: walk to and pick up valuable items from the ground."""

    def __init__(self, items: list, ss) -> None:
        self.name = "scavenge_ground_items"
        self.description = "Pick up valuable items from ground"
        self._items = items
        self._player_pos = (ss.x, ss.y)

    async def can_start(self, ctx) -> bool:
        return True

    async def run(self, ctx) -> ProcedureResult:
        from anima.action.movement import go_to
        from anima.client.packets import build_drop_item, build_pick_up

        ss = ctx.perception.self_state
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no backpack",
            )

        picked = 0
        for item_ref in self._items:
            # Re-check item still exists on ground
            item = ctx.perception.world.items.get(item_ref.serial)
            if not item or item.container != 0:
                continue

            # Check weight limit
            if ss.weight_max > 0 and ss.weight > ss.weight_max - 50:
                break

            # Walk to item if not adjacent
            dist = max(abs(item.x - ss.x), abs(item.y - ss.y))
            if dist > 2:
                arrived = await go_to(ctx, item.x, item.y)
                if not arrived:
                    continue  # skip unreachable items
                # Re-check distance after walking — go_to may arrive
                # near but not within UO's 2-tile pickup range
                dist = max(abs(item.x - ss.x), abs(item.y - ss.y))
                if dist > 2:
                    continue

            # Pick up into backpack
            await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
            await asyncio.sleep(0.3)
            await ctx.conn.send_packet(
                build_drop_item(item.serial, container=backpack)
            )
            await asyncio.sleep(0.3)
            picked += 1
            logger.info(
                "scavenged_item",
                serial=f"0x{item.serial:08X}",
                graphic=f"0x{item.graphic:04X}",
                amount=item.amount,
            )

            if picked >= 5:
                break  # don't spend too long scavenging

        if picked == 0:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no items could be picked up",
            )

        # Figure out appropriate next step based on what we picked up
        from anima.skills.gathering.mine import ORE_GRAPHICS

        has_ore = any(
            it.graphic in ORE_GRAPHICS
            for it in ctx.perception.world.items.values()
            if it.container == backpack
        )
        hint = "smelt_ore" if has_ore else None

        return ProcedureResult(
            success=True,
            message=f"Scavenged {picked} items from ground",
            next_suggestion=hint,
        )
