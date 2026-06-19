"""SmeltOre procedure — convert ore into ingots at a forge."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.target import use_on_object, use_on_target
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.crafting.smelt import (
    INGOT_GRAPHICS,
    ORE_GRAPHICS,
    _find_forge_dynamic,
    _find_forge_static,
)

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


class SmeltOre(Procedure):
    name = "smelt_ore"
    description = "Double-click ore and target a forge to smelt into ingots."

    async def can_start(self, ctx: AgentContext) -> bool:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return False

        unsmelable = ctx.blackboard.get("_unsmelable_ore_hues", set())
        small_iron = ctx.blackboard.get("_small_iron_ore_serials", set())

        has_ore = any(
            it.graphic in ORE_GRAPHICS and it.hue not in unsmelable
            and not (it.serial in small_iron and it.amount < 2)
            for it in world.items.values()
            if it.container == backpack
        )
        if not has_ore:
            # Also check ground nearby (excluding junk serials)
            junk = ctx.blackboard.get("_junk_ore_serials", set())
            has_ore = any(
                it.graphic in ORE_GRAPHICS and it.container == 0
                and it.hue not in unsmelable
                and it.serial not in junk
                and max(abs(it.x - ss.x), abs(it.y - ss.y)) <= 2
                for it in world.items.values()
            )
        if not has_ore:
            return False

        return _find_forge_dynamic(ctx) is not None or _find_forge_static(ctx) is not None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        backpack = ss.equipment.get(0x15)

        # Find ore (skip hues we've proven unsmelable, smelt smallest piles
        # first — ServUO consumes the whole targeted pile per attempt, so
        # small piles first means more smelt attempts (more Mining gain
        # rolls) and less ore wasted on failures)
        unsmelable = ctx.blackboard.get("_unsmelable_ore_hues", set())
        small_iron = ctx.blackboard.get("_small_iron_ore_serials", set())
        ore_candidates = [
            item for item in world.items.values()
            if (item.container == backpack and item.graphic in ORE_GRAPHICS
                and item.hue not in unsmelable
                and not (item.serial in small_iron and item.amount < 2))
        ]
        # Ascending among piles with amount >= 2; sub-2 piles sort last so
        # they're only targeted when nothing else remains (the small-pile
        # combine logic in the failure path then handles them).
        ore_candidates.sort(key=lambda x: (x.amount < 2, x.amount))
        ore = ore_candidates[0] if ore_candidates else None

        if not ore:
            # Pick up from ground (skip unsmelable hues and junk serials)
            junk = ctx.blackboard.get("_junk_ore_serials", set())
            from anima.client.packets import build_drop_item, build_pick_up
            for item in world.items.values():
                if (item.graphic in ORE_GRAPHICS and item.container == 0
                        and item.hue not in unsmelable
                        and item.serial not in junk
                        and max(abs(item.x - ss.x), abs(item.y - ss.y)) <= 2):
                    await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                    await asyncio.sleep(0.3)
                    await ctx.conn.send_packet(
                        build_drop_item(item.serial, 0xFFFF, 0xFFFF, 0, backpack)
                    )
                    await asyncio.sleep(0.5)
                    ore = item
                    break

        if not ore:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no ore",
            )

        # Find forge
        forge_dyn = _find_forge_dynamic(ctx)
        forge_sta = _find_forge_static(ctx)
        if not forge_dyn and not forge_sta:
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="no forge nearby",
            )

        # Walk to forge if needed
        if forge_dyn:
            fx, fy = forge_dyn[0], forge_dyn[1]
        else:
            fx, fy = forge_sta[0], forge_sta[1]  # type: ignore[index]

        dist = max(abs(fx - ss.x), abs(fy - ss.y))
        if dist > 1:
            from anima.action.movement import go_to
            arrived = await go_to(ctx, fx, fy)
            if not arrived:
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.BLOCKED,
                    message=f"could not reach forge ({fx},{fy})",
                )
            forge_dyn = _find_forge_dynamic(ctx)
            forge_sta = _find_forge_static(ctx)

        # Count ingots before
        ingots_before = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in INGOT_GRAPHICS
        )

        # Listen for server feedback during smelting
        _smelt_flags = {"not_enough": False}

        def _on_speech(_topic: str, data: dict) -> None:
            text = data.get("text", "")
            if "not enough metal" in text.lower():
                _smelt_flags["not_enough"] = True

        sub1 = sub2 = None
        if ctx.bus:
            sub1 = ctx.bus.subscribe("avatar.speech_heard", _on_speech)
            sub2 = ctx.bus.subscribe("avatar.speech_cliloc", _on_speech)

        # ServUO Ore.cs smelt result clilocs (Scripts/Items/Resource/Ore.cs).
        # Any of these means the smelt attempt RESOLVED, so the per-swing
        # wait can break immediately instead of always napping a flat 2.0s.
        # The flat nap was the dominant dead-time sink in the mine->smelt
        # loop: every smelt — success or instant skill/type failure — paid
        # the full 2.0s even though the server answered in well under a
        # second, and this procedure re-suggests itself (next_suggestion=
        # "smelt_ore"), so the waste compounds across the whole tour.
        _result_snippets = (
            "put the metal in your backpack",   # 501988 success
            "burn away the impurities",         # 501990 partial success
            "not enough metal",                 # 501987 too small a pile
            "no idea how to smelt",             # 501986 unsmelable type
        )
        # Success lines specifically. Like MineOre / ChopWood, ServUO sends the
        # smelt-success cliloc ("...put the metal in your backpack.") and the
        # container-content update (0x25/0x1A) that actually adds the ingots as
        # two separate packets whose relative order is NOT guaranteed — the
        # message is frequently processed first. If the wait loop breaks on the
        # success line before the ingot item has landed in world.items,
        # ingots_after == ingots_before and a genuinely successful smelt is
        # mis-booked as a failure with zero yield credited. Track it so we can
        # grace-poll for the item update before declaring failure.
        _success_snippets = (
            "put the metal in your backpack",   # 501988 success
            "burn away the impurities",         # 501990 partial success
        )

        def _journal_result_seen() -> bool:
            for entry in ctx.perception.social.journal:
                if entry.timestamp < smelt_start:
                    continue
                tl = entry.text.lower()
                if any(s in tl for s in _result_snippets):
                    return True
            return False

        def _journal_success_seen() -> bool:
            for entry in ctx.perception.social.journal:
                if entry.timestamp < smelt_start:
                    continue
                tl = entry.text.lower()
                if any(s in tl for s in _success_snippets):
                    return True
            return False

        smelt_start = time.time()
        # Smelt: double-click ore → target forge
        if forge_dyn:
            result = await use_on_object(ctx, ore.serial, forge_dyn[3])
        else:
            fx, fy, fz, fg = forge_sta  # type: ignore[misc]
            result = await use_on_target(ctx, ore.serial, fx, fy, fz, graphic=fg)

        if not result.success:
            if ctx.bus:
                if sub1:
                    ctx.bus.unsubscribe(sub1)
                if sub2:
                    ctx.bus.unsubscribe(sub2)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=result.message,
            )

        # Event-driven cadence (mirrors MineOre / ChopWood): poll until the
        # smelt resolves — new ingots in the pack, the "not enough metal"
        # flag, or a smelt-result journal line — and only burn the full
        # window on a true timeout.
        def _ingots_in_pack() -> int:
            return sum(
                it.amount for it in world.items.values()
                if it.container == backpack and it.graphic in INGOT_GRAPHICS
            )

        deadline = smelt_start + 2.0
        while time.time() < deadline:
            await asyncio.sleep(0.2)
            if (_ingots_in_pack() > ingots_before
                    or _smelt_flags["not_enough"]
                    or _journal_result_seen()):
                break

        if ctx.bus:
            if sub1:
                ctx.bus.unsubscribe(sub1)
            if sub2:
                ctx.bus.unsubscribe(sub2)

        # The success cliloc can arrive before the item-update packet that
        # actually adds the ingots to the pack. If the smelt reported success
        # but the ingots are not visible yet (and the "not enough metal" flag
        # did not fire), grace-poll briefly for the container update so the
        # yield is credited instead of being lost as a phantom failure. Without
        # this, a successful colored-ore smelt that lost the race is counted as
        # a miss; three such misses blacklist the hue (_unsmelable_ore_hues)
        # and the agent permanently DROPS perfectly-smeltable ore as junk.
        if (_ingots_in_pack() <= ingots_before
                and not _smelt_flags["not_enough"]
                and _journal_success_seen()):
            grace_deadline = time.time() + 1.0
            while time.time() < grace_deadline:
                await asyncio.sleep(0.1)
                if _ingots_in_pack() > ingots_before:
                    break

        ingots_after = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in INGOT_GRAPHICS
        )
        ingots_gained = ingots_after - ingots_before

        ore_hue = ore.hue
        ore_serial = ore.serial
        ore_amount = ore.amount

        if ingots_gained > 0:
            # Reset fail counter for this ore hue on success
            fail_counts = ctx.blackboard.get("_smelt_fail_counts", {})
            fail_counts.pop(ore_hue, None)
            ctx.blackboard.pop("_small_iron_ore_serials", None)
            return ProcedureResult(
                success=True,
                message=f"Smelted {ingots_gained} ingots",
                next_suggestion="smelt_ore",
                details={"ingots": ingots_gained},
            )

        # --- Smelting failed ---
        # Iron ore (hue 0) is always smeltable at any mining skill —
        # failures are random skill checks, never permanent.  Don't
        # blacklist it; just retry indefinitely.
        if ore_hue == 0:
            if _smelt_flags["not_enough"] and ore_amount < 2:
                # Pile too small to smelt — try combining with another iron ore pile
                other_iron = next(
                    (item for item in world.items.values()
                     if (item.container == backpack and item.graphic in ORE_GRAPHICS
                         and item.hue == 0 and item.serial != ore_serial)),
                    None,
                )
                if other_iron:
                    # Merge same-type stacks with a lift + drop-onto-stack
                    # (the UO stack-combine), NOT use_on_object: double-
                    # clicking ore begins a *smelt* (Ore.OnDoubleClick ->
                    # Smelt), so use_on_object would re-open the smelt cursor
                    # and target the other pile, merging nothing. Dropping the
                    # lifted pile with container=other_iron.serial stacks them.
                    from anima.client.packets import build_drop_item, build_pick_up
                    await ctx.conn.send_packet(build_pick_up(ore_serial, ore_amount))
                    await asyncio.sleep(0.3)
                    await ctx.conn.send_packet(
                        build_drop_item(ore_serial, container=other_iron.serial)
                    )
                    await asyncio.sleep(0.5)
                    logger.info("smelt_combined_ore",
                                from_serial=hex(ore_serial),
                                to_serial=hex(other_iron.serial))
                    return ProcedureResult(
                        success=False,
                        reason=FailureReason.BLOCKED,
                        message="Combined small ore piles, retrying",
                        next_suggestion="smelt_ore",
                    )
                # No other pile or combine failed — skip this serial
                small_set = ctx.blackboard.setdefault("_small_iron_ore_serials", set())
                small_set.add(ore_serial)
                logger.info("smelt_iron_too_small",
                            serial=hex(ore_serial), amount=ore_amount)
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.BLOCKED,
                    message=f"Iron ore pile too small ({ore_amount}), skipped",
                )
            fail_counts = ctx.blackboard.setdefault("_smelt_fail_counts", {})
            fail_count = fail_counts.get(0, 0) + 1
            fail_counts[0] = fail_count
            logger.info("smelt_iron_retry", fail_count=fail_count)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"Iron smelting failed (retry {fail_count})",
            )

        # "not enough metal" with sufficient quantity = ore type is truly
        # unsmelable at current skill → blacklist immediately.
        # With small amounts (< 2), the failure is a quantity issue (e.g.
        # 1 iron ore on a shard that requires 2+), not a type issue —
        # use the 3-strike counter so we retry once more ore accumulates.
        immediate_blacklist = _smelt_flags["not_enough"] and ore_amount >= 2

        fail_counts = ctx.blackboard.setdefault("_smelt_fail_counts", {})
        fail_count = fail_counts.get(ore_hue, 0) + 1
        fail_counts[ore_hue] = fail_count

        if immediate_blacklist or fail_count >= 3:
            fail_counts.pop(ore_hue, None)
            unsmelable_set = ctx.blackboard.setdefault(
                "_unsmelable_ore_hues", set()
            )
            unsmelable_set.add(ore_hue)

            # Drop only ore of this hue (not all ore)
            from anima.client.packets import build_drop_item, build_pick_up
            dropped = 0
            for item in list(world.items.values()):
                if (item.container == backpack
                        and item.graphic in ORE_GRAPHICS
                        and item.hue == ore_hue):
                    await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                    await asyncio.sleep(0.3)
                    await ctx.conn.send_packet(
                        build_drop_item(item.serial, ss.x, ss.y, ss.z)
                    )
                    await asyncio.sleep(0.3)
                    dropped += 1
            # Mark dropped ore as junk so planner won't pick them up
            junk = ctx.blackboard.setdefault("_junk_ore_serials", set())
            for item in world.items.values():
                if (item.container == 0 and item.graphic in ORE_GRAPHICS
                        and item.hue == ore_hue
                        and max(abs(item.x - ss.x), abs(item.y - ss.y)) <= 2):
                    junk.add(item.serial)

            reason_str = "not enough metal" if immediate_blacklist else f"{fail_count} failures"
            logger.info(
                "smelt_gave_up_ore",
                dropped=dropped, hue=ore_hue, reason=reason_str,
            )
            return ProcedureResult(
                success=False,
                reason=FailureReason.PERMANENT,
                message=f"Ore hue {ore_hue} unsmelable ({reason_str}), dropped {dropped} stacks",
            )

        return ProcedureResult(
            success=False,
            reason=FailureReason.BLOCKED,
            message=f"Smelting failed ({fail_count}/3)",
        )
