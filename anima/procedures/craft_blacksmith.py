"""CraftBlacksmith procedure — craft weapons/armor at anvil to sell for profit.

Uses tongs + ingots at a forge/anvil. Crafts items based on current skill level.
Uses computed ServUO button IDs (not text matching) for reliable gump navigation.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.gump import wait_for_gump
from anima.actions.inventory import find_in_backpack
from anima.client.packets import build_double_click, build_gump_response
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.crafting.blacksmith import ANVIL_IDS, FORGE_IDS
from anima.skills.crafting.smelt import INGOT_GRAPHICS

if TYPE_CHECKING:
    from anima.core.context import AgentContext
    from anima.perception.self_state import SelfState

logger = structlog.get_logger()

TONGS_GRAPHICS = {0x0FBB, 0x0FBC}  # smith's hammer / tongs
INGOT_GRAPHIC = 0x1BF2  # kept for backward compat (large-stack graphic)
IRON_HUE = 0  # Iron ingots have default (no) hue; colored metals have non-zero hue
MIN_INGOTS = 8  # most weapons need 8-12 ingots
# Yield the forge to a sell trip once this many crafted items have piled up.
# Without it, can_start stays True forever (first-startable-wins) and the
# crafter never advances to sell_to_vendor — worth_term/networth_rate stay
# structurally 0 while produce_term balloons (the CRAFTING worth=0 plateau).
SELL_BACKLOG_THRESHOLD = 10


def _count_iron_ingots(ctx: AgentContext) -> int:
    """Count only iron ingots (hue 0) in backpack, excluding colored metals."""
    ss = ctx.perception.self_state
    backpack = ss.equipment.get(0x15)
    if not backpack:
        return 0
    return sum(
        item.amount
        for item in ctx.perception.world.items.values()
        if item.container == backpack
        and item.graphic in INGOT_GRAPHICS
        and item.hue == IRON_HUE
    )


def _get_button_id(btn_type: int, index: int) -> int:
    """ServUO CraftGump button ID formula: 1 + type + (index * 7).

    type 0 = show group (category), type 1 = create item.
    """
    return 1 + btn_type + (index * 7)


# MAKE LAST misc button: GetButtonID(6, 2) = 1 + 6 + 2*7 = 21.
# ServUO CraftGump.cs OnResponse → case 6 (misc buttons) → case 2 "Make last":
# re-crafts context.LastMade without re-navigating material/group/item pages.
MAKE_LAST_BUTTON = _get_button_id(6, 2)  # 21

MAX_BATCH_ATTEMPTS = 80   # sane cap per procedure run
_RESULT_TIMEOUT_S = 6.0   # craft delay is ~2s; past 6s the re-sent gump is lost
_TIMEOUT_MARGIN_S = 12.0  # stop batching this long before the planner cap


def _classify_craft_text(text: str) -> str:
    """Map a craft notice / journal line to an outcome token ('' if none).

    Strings are the ServUO clilocs the shard attaches to the re-sent
    CraftGump notice (or sends as journal messages when the tool broke):
    1044154/1044155/1044156 "You create …", 502785 "barely able to make",
    1044043 "You failed to create …", 1044157 "You fail to create …",
    1044038 "You have worn out your tool!", 1044037 "… sufficient metal …".
    """
    tl = text.lower()
    if "worn out" in tl:
        return "tool_broke"
    if "sufficient metal" in tl or "sufficient material" in tl:
        return "no_material"
    if "failed to create" in tl or "you fail" in tl:
        return "fail"
    if "you create" in tl or "barely able to make" in tl:
        return "success"
    return ""


# Recipes: (item_name, group_index, item_index, ingots_needed, min_skill)
# Group/item indices verified against ServUO DefBlacksmithy.cs under
# CurrentExpansion=EJ (this shard): all era-gated AddCrafts are active, and
# ring/chain/platemail are merged into group 0 "Metal Armor". Bladed (group 3)
# order: 0=BoneHarvester 1=Broadsword 2=CrescentBlade 3=Cutlass 4=Dagger
# 5=Katana 6=Kryss 7=Longsword 8=Scimitar 9=VikingSword …
# The old table clicked index (3,0) believing it was Cutlass and crafted
# BoneHarvester (minSkill 33) instead — 4% success at skill 35, 10 ingots
# burned per failure.
_RECIPES = [
    ("Dagger", 3, 4, 3, -0.4),
    ("Ringmail Gloves", 0, 0, 10, 12.0),
    ("Ringmail Sleeves", 0, 2, 14, 16.9),
    ("Ringmail Leggings", 0, 1, 16, 19.4),
    ("Ringmail Tunic", 0, 3, 18, 21.9),
    ("Cutlass", 3, 3, 8, 24.3),
    ("Longsword", 3, 7, 12, 28.0),
    ("Scimitar", 3, 8, 10, 31.7),
    ("Broadsword", 3, 1, 10, 35.4),
    ("Kryss", 3, 6, 8, 36.7),
    ("Katana", 3, 5, 8, 44.1),
    ("Crescent Blade", 3, 2, 14, 45.0),
]

# Graphics of items we crafted (to detect in inventory and to sell)
CRAFTED_WEAPON_GRAPHICS = {
    0x0F51, 0x0F52,  # dagger
    0x1441,  # cutlass
    0x13FF,  # katana
    0x1400, 0x1401,  # kryss
    0x0F61,  # longsword
    0x13B6,  # scimitar
    0x0F5E,  # broadsword
    0x26C1, 0x26CB,  # crescent blade
    0x1405,  # war fork
}
CRAFTED_ARMOR_GRAPHICS = {
    0x13EB,  # ringmail gloves
    0x13F0,  # ringmail leggings
    0x13EE,  # ringmail sleeves
    0x13EC,  # ringmail tunic
}
CRAFTED_ITEM_GRAPHICS = CRAFTED_WEAPON_GRAPHICS | CRAFTED_ARMOR_GRAPHICS


def _count_crafted_in_pack(ctx: AgentContext) -> int:
    """Count crafted weapons/armor sitting in the backpack (the sell backlog)."""
    ss = ctx.perception.self_state
    backpack = ss.equipment.get(0x15)
    if not backpack:
        return 0
    return sum(
        getattr(item, "amount", 1)
        for item in ctx.perception.world.items.values()
        if item.container == backpack and item.graphic in CRAFTED_ITEM_GRAPHICS
    )


_CRAFT_SEARCH_RANGE = 8  # Wider range for walk-to-forge feature


def _find_nearest(
    ctx: AgentContext, graphic_ids: set[int], distance: int,
) -> tuple[int, int] | None:
    """Find the nearest item/static matching graphic_ids within distance tiles."""
    ss = ctx.perception.self_state
    best: tuple[int, int] | None = None
    best_dist = distance + 1

    for it in ctx.perception.world.nearby_items(ss.x, ss.y, distance=distance):
        if it.graphic in graphic_ids:
            d = max(abs(it.x - ss.x), abs(it.y - ss.y))
            if d < best_dist:
                best = (it.x, it.y)
                best_dist = d

    if ctx.map_reader is not None:
        for dy in range(-distance, distance + 1):
            for dx in range(-distance, distance + 1):
                d = max(abs(dx), abs(dy))
                if d >= best_dist:
                    continue
                tile = ctx.map_reader.get_tile(ss.x + dx, ss.y + dy)
                for s in tile.statics:
                    if s.graphic in graphic_ids:
                        best = (ss.x + dx, ss.y + dy)
                        best_dist = d
                        break

    return best


def _has_anvil_and_forge(ctx: AgentContext) -> bool:
    """Check that both an anvil and a forge are within 2 tiles."""
    return (_find_nearest(ctx, ANVIL_IDS, 2) is not None
            and _find_nearest(ctx, FORGE_IDS, 2) is not None)


def _has_anvil_and_forge_nearby(ctx: AgentContext) -> bool:
    """Check that both an anvil and a forge exist within walk-to range.

    Also verifies the two are close enough (Chebyshev ≤ 4) that a midpoint
    walk target will land within 2 tiles of both.
    """
    if _has_anvil_and_forge(ctx):
        return True
    anvil = _find_nearest(ctx, ANVIL_IDS, _CRAFT_SEARCH_RANGE)
    forge = _find_nearest(ctx, FORGE_IDS, _CRAFT_SEARCH_RANGE)
    if not anvil or not forge:
        return False
    dist = max(abs(anvil[0] - forge[0]), abs(anvil[1] - forge[1]))
    return dist <= 4


def _find_craft_walk_target(ctx: AgentContext) -> tuple[int, int] | None:
    """Find a position to walk to that puts us within 2 tiles of both forge and anvil.

    Returns (x, y) or None.  Walks toward the anvil since forges are
    typically multi-tile and easier to be near.
    """
    anvil = _find_nearest(ctx, ANVIL_IDS, _CRAFT_SEARCH_RANGE)
    forge = _find_nearest(ctx, FORGE_IDS, _CRAFT_SEARCH_RANGE)
    if not anvil or not forge:
        logger.info(
            "craft_walk_target_none",
            has_anvil=anvil is not None,
            has_forge=forge is not None,
            pos=f"({ctx.perception.self_state.x},{ctx.perception.self_state.y})",
            map_reader=ctx.map_reader is not None,
        )
        return None

    mx, my = (anvil[0] + forge[0]) // 2, (anvil[1] + forge[1]) // 2
    if (max(abs(mx - anvil[0]), abs(my - anvil[1])) > 2
            or max(abs(mx - forge[0]), abs(my - forge[1])) > 2):
        logger.info(
            "craft_walk_target_too_far",
            forge=f"({forge[0]},{forge[1]})",
            anvil=f"({anvil[0]},{anvil[1]})",
            midpoint=f"({mx},{my})",
        )
        return None
    logger.info(
        "craft_walk_target",
        forge=f"({forge[0]},{forge[1]})",
        anvil=f"({anvil[0]},{anvil[1]})",
        target=f"({mx},{my})",
    )
    return (mx, my)


class CraftBlacksmith(Procedure):
    name = "craft_blacksmith"
    description = "Craft weapons or armor from ingots to sell for profit."
    # Matches the planner default, pinned here so the batch loop can budget
    # against it (stop batching _TIMEOUT_MARGIN_S before the cancel cap).
    timeout_s = 300.0

    async def can_start(self, ctx: AgentContext) -> bool:
        # CircuitBreaker check (preferred)
        breaker = ctx.blackboard.get("_craft_material_breaker")
        if breaker is not None and breaker.is_open("iron"):
            return False
        # Legacy fallback for tests that don't install the breaker
        if time.time() < ctx.blackboard.get("_craft_bs_material_cooldown", 0):
            return False
        if time.time() < ctx.blackboard.get("_craft_bs_location_cooldown", 0):
            return False
        if not find_in_backpack(ctx, TONGS_GRAPHICS):
            return False
        if _count_iron_ingots(ctx) < MIN_INGOTS:
            return False
        # Vendor-flush: once a sellable batch has piled up, yield so the
        # profession loop advances to sell_to_vendor (converts produce_term
        # into worth_term/gold instead of crafting indefinitely).
        if _count_crafted_in_pack(ctx) >= SELL_BACKLOG_THRESHOLD:
            return False
        # Accept if forge+anvil are within 2 tiles (ready to craft)
        # OR within 8 tiles (can walk there during execute).
        return _has_anvil_and_forge_nearby(ctx)

    def _pick_recipe(self, ctx: AgentContext) -> tuple[str, int, int, int] | None:
        """Pick best recipe based on skill level and available ingots.

        Returns (item_name, group_index, item_index, ingot_cost) or None.
        """
        ingots = _count_iron_ingots(ctx)
        skill = 0.0
        for sk in ctx.perception.self_state.skills.values():
            if sk.id == 7:  # Blacksmithy
                skill = sk.value
                break

        # ServUO success chance is (skill - minSkill) / (maxSkill - minSkill)
        # with a 50-point window. Prefer recipes in the trainable band
        # (25-90% success — gains keep coming AND items get made), cheapest
        # ingot cost first; otherwise fall back to the highest success rate.
        # Failed crafts consume the FULL ingot cost, so cost matters.
        affordable = [
            (name, grp, idx, cost, min(1.0, max(0.0, (skill - min_sk) / 50.0)))
            for name, grp, idx, cost, min_sk in _RECIPES
            if ingots >= cost
        ]
        affordable = [r for r in affordable if r[4] > 0.0]
        if not affordable:
            return None
        trainable = [r for r in affordable if 0.25 <= r[4] <= 0.90]
        if trainable:
            name, grp, idx, cost, _ = min(trainable, key=lambda r: r[3])
        elif max(r[4] for r in affordable) > 0.90:
            # Skill has outgrown the training band: every affordable recipe is
            # near-certain (success clamps to ~1.0). max(..., key=success) would
            # tie on 1.0 and return the FIRST recipe in list order — the Dagger
            # (3 ingots, near-worthless) — so a GM smith spammed trash and
            # worth_term/networth_rate stayed ~0 (the CRAFTING worth plateau).
            # Pick the most VALUABLE reliable item instead; ingot cost is a good
            # proxy for sell value (heavier armor/weapons sell for more).
            reliable = [r for r in affordable if r[4] > 0.90]
            name, grp, idx, cost, _ = max(reliable, key=lambda r: r[3])
        else:
            # All affordable recipes are below the training band: maximise the
            # odds of actually making something (least ingots burned on fails).
            name, grp, idx, cost, _ = max(affordable, key=lambda r: r[4])
        return name, grp, idx, cost

    async def _close_all_gumps(self, ctx: AgentContext) -> None:
        """Close all open gumps to prevent stale state on the server."""
        ss = ctx.perception.self_state
        for g in list(ss.gumps.values()):
            ss.gumps.pop(g.gump_id, None)
            await ctx.conn.send_packet(
                build_gump_response(g.serial, g.gump_id, 0)
            )

    @staticmethod
    def _extract_gump_notice(ss: SelfState) -> str:
        """Read the notice line from an open CraftGump, or ''.

        ServUO renders the notice at AddHtmlLocalized(170, 295, …)
        (CraftGump.cs); skip the "NOTICES" header label at x=10, y=302
        (cliloc 1044012).
        """
        for g in ss.gumps.values():
            for t in g.texts:
                if 280 <= t.y <= 310 and t.x >= 150:
                    text = g.get_text(t.text_id)
                    if text:
                        return re.sub(r"<[^>]+>", "", text).strip()
        return ""

    async def _await_craft_result(
        self,
        ctx: AgentContext,
        journal_mark: float,
        timeout: float = _RESULT_TIMEOUT_S,
    ) -> tuple[str, str]:
        """Wait for a craft attempt to resolve and classify the outcome.

        ServUO re-sends the CraftGump with a result notice when the attempt
        resolves (CraftItem.CompleteCraft); if the tool broke it is deleted,
        so only a journal line arrives — no gump. Waits on bus events rather
        than fixed sleeps where possible.

        Returns ``(outcome, notice)``; outcome is one of: ``success`` /
        ``fail`` / ``tool_broke`` / ``no_material`` / ``no_station`` /
        ``unknown`` (gump arrived, notice unrecognized) / ``''`` (nothing
        arrived before *timeout*).
        """
        ss = ctx.perception.self_state

        def _journal_outcomes() -> set[str]:
            found: set[str] = set()
            for entry in ctx.perception.social.recent(count=8):
                if entry.timestamp >= journal_mark:
                    outcome = _classify_craft_text(entry.text)
                    if outcome:
                        found.add(outcome)
            return found

        def _resolved() -> bool:
            return bool(ss.gumps) or bool(_journal_outcomes())

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "", ""
            if ctx.bus:
                if not await ctx.bus.wait_for_condition(_resolved, timeout=remaining):
                    return "", ""
            elif not _resolved():
                await asyncio.sleep(0.25)
                continue

            notice = self._extract_gump_notice(ss)
            notice_outcome = _classify_craft_text(notice) if notice else ""
            nl = notice.lower()
            if not notice_outcome and ("anvil" in nl or "forge" in nl):
                notice_outcome = "no_station"
            journal = _journal_outcomes()

            # Priority: tool_broke > no_material/no_station > fail > success
            # (a failing craft can break the tool in the same attempt).
            if "tool_broke" in journal or notice_outcome == "tool_broke":
                return "tool_broke", notice
            if notice_outcome in ("no_material", "no_station") or "no_material" in journal:
                return notice_outcome or "no_material", notice
            if "fail" in journal or notice_outcome == "fail":
                return "fail", notice
            if "success" in journal or notice_outcome == "success":
                return "success", notice
            if ss.gumps:
                # Gump re-arrived but the notice is unrecognized or empty
                return "unknown", notice
            # Spurious wake (e.g. gump consumed between checks) — retry
            await asyncio.sleep(0.1)

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        exec_start = time.monotonic()

        # Close stale gumps from previous failed runs
        await self._close_all_gumps(ctx)

        tools = find_in_backpack(ctx, TONGS_GRAPHICS)
        if not tools:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no tongs",
            )

        if not _has_anvil_and_forge(ctx):
            # Not within crafting range — try walking to nearby forge/anvil
            target = _find_craft_walk_target(ctx)
            if target:
                from anima.action.movement import go_to
                arrived = await go_to(ctx, target[0], target[1], exact=True)
                if not arrived or not _has_anvil_and_forge(ctx):
                    fails = ctx.blackboard.get("_craft_bs_location_fails", 0) + 1
                    ctx.blackboard["_craft_bs_location_fails"] = fails
                    if fails >= 3:
                        ctx.blackboard["_craft_bs_location_cooldown"] = time.time() + 120
                        ctx.blackboard["_craft_bs_location_fails"] = 0
                        logger.warning("craft_bs_location_cooldown", fails=fails)
                    return ProcedureResult(
                        success=False,
                        reason=FailureReason.WRONG_LOCATION,
                        message=f"walked to ({target[0]},{target[1]}) but still"
                                " no anvil/forge within 2 tiles",
                    )
                logger.info("craft_bs_walked_to_forge", target=f"({target[0]},{target[1]})")
            else:
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.WRONG_LOCATION,
                    message="no anvil or forge within 8 tiles",
                )

        recipe = self._pick_recipe(ctx)
        if not recipe:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no suitable recipe (skill too low or not enough ingots)",
            )

        item_name, grp_idx, item_idx, ingot_cost = recipe
        logger.info(
            "craft_bs_start",
            item=item_name, group=grp_idx, item_index=item_idx,
            ingot_cost=ingot_cost, ingots=_count_iron_ingots(ctx),
            skill=next((sk.value for sk in ss.skills.values() if sk.id == 7), 0),
        )

        # Count items before crafting
        items_before = len([
            it for it in ctx.perception.world.items.values()
            if it.container == ss.equipment.get(0x15) and it.graphic in CRAFTED_ITEM_GRAPHICS
        ])
        ingots_before = _count_iron_ingots(ctx)

        # First attempt: full gump navigation (open → Iron → group → item).
        journal_mark = time.time()
        nav_fail = await self._open_gump_and_craft(ctx, grp_idx, item_idx)
        if nav_fail is not None:
            return nav_fail

        # Batch loop — ServUO re-sends the CraftGump with a result notice
        # after every attempt resolves, so each further attempt is a single
        # MAKE LAST click on the re-sent gump instead of a full menu
        # re-navigation (~5-7s of overhead → ~2s per attempt).
        deadline = exec_start + (self.timeout_s or 300.0) - _TIMEOUT_MARGIN_S
        attempts = 0
        successes = 0
        fails = 0
        renavigated = False
        stop_reason = ""
        last_notice = ""

        async def _renavigate_once() -> bool:
            """Fallback when the gump is lost: redo the full navigation once."""
            nonlocal renavigated, journal_mark
            if renavigated:
                return False
            renavigated = True
            logger.info("craft_bs_renavigate", attempts=attempts)
            await self._close_all_gumps(ctx)
            journal_mark = time.time()
            return (await self._open_gump_and_craft(ctx, grp_idx, item_idx)) is None

        while True:
            attempts += 1
            outcome, last_notice = await self._await_craft_result(ctx, journal_mark)

            if outcome == "success":
                successes += 1
            elif outcome == "fail":
                fails += 1
            elif outcome == "tool_broke":
                stop_reason = "tool_broke"
                break
            elif outcome in ("no_material", "no_station"):
                stop_reason = outcome
                break
            else:  # '' (no result within ~6s) or 'unknown' (unreadable notice)
                if await _renavigate_once():
                    continue
                stop_reason = outcome or "no_gump"
                break

            # Exit checks before queuing the next attempt
            if attempts >= MAX_BATCH_ATTEMPTS:
                stop_reason = "attempt_cap"
                break
            if time.monotonic() > deadline:
                stop_reason = "timeout"
                break
            if _count_iron_ingots(ctx) < ingot_cost:
                stop_reason = "out_of_ingots"
                break

            # The result usually rides in on the re-sent gump; if the outcome
            # came from the journal first, give the gump a moment to land.
            if not ss.gumps:
                gres = await wait_for_gump(ctx, timeout=3.0)
                if not gres.success:
                    if await _renavigate_once():
                        continue
                    stop_reason = "gump_lost"
                    break
            gump = ss.gumps[max(ss.gumps.keys())]
            if not gump.find_button_by_id(MAKE_LAST_BUTTON):
                stop_reason = "no_make_last_button"
                break

            journal_mark = time.time()
            ss.gumps.clear()
            switches = [sw.switch_id for sw in gump.switches if sw.initial_state]
            text_entries = [
                (te.entry_id, te.initial_text) for te in gump.text_entries
            ]
            await ctx.conn.send_packet(build_gump_response(
                serial=gump.serial, gump_id=gump.gump_id,
                button_id=MAKE_LAST_BUTTON,
                switches=switches, text_entries=text_entries,
            ))

        ingots_after = _count_iron_ingots(ctx)
        items_after = len([
            it for it in ctx.perception.world.items.values()
            if it.container == ss.equipment.get(0x15)
            and it.graphic in CRAFTED_ITEM_GRAPHICS
        ])
        crafted = max(successes, items_after - items_before)
        consumed = max(0, ingots_before - ingots_after)

        # Close remaining gumps
        await self._close_all_gumps(ctx)

        if crafted > 0:
            logger.info(
                "craft_blacksmith_batch",
                item=item_name, crafted=crafted, failed=fails,
                attempts=attempts, ingots_used=consumed, stop=stop_reason,
            )
            ctx.blackboard["_craft_bs_fails"] = 0
            ctx.blackboard["_craft_bs_material_fails"] = 0
            ctx.blackboard["_craft_bs_location_fails"] = 0
            breaker = ctx.blackboard.get("_craft_material_breaker")
            if breaker is not None:
                breaker.record_success("iron")
            msg = (f"Crafted {crafted}x {item_name} in {attempts} attempts "
                   f"({fails} failed, {consumed} ingots)")
            if stop_reason == "tool_broke":
                msg += " — tongs broke"
            keep_going = stop_reason not in (
                "tool_broke", "out_of_ingots", "no_material",
            )
            return ProcedureResult(
                success=True,
                message=msg,
                next_suggestion="craft_blacksmith" if keep_going else None,
                details={
                    "item": item_name, "crafted": crafted, "failed": fails,
                    "attempts": attempts, "ingots_used": consumed,
                    "stop": stop_reason,
                },
            )

        if stop_reason == "tool_broke":
            logger.warning("craft_blacksmith_tool_broke")
            ctx.blackboard["_craft_bs_fails"] = 0
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="Tongs broke — need replacement tool",
            )

        if fails > 0 or ingots_after < ingots_before:
            lost = ingots_before - ingots_after
            logger.info(
                "craft_blacksmith_failed",
                item=item_name, ingots_lost=lost, attempts=attempts,
            )
            ctx.blackboard["_craft_bs_fails"] = 0
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"Craft {item_name} failed — lost {lost} ingots (skill check)",
                next_suggestion="craft_blacksmith",
            )

        notice_lower = last_notice.lower()

        if (stop_reason == "no_material" or "sufficient metal" in notice_lower
                or "sufficient material" in notice_lower):
            # If we counted enough iron ingots but server still says insufficient,
            # the craft context likely has the wrong material selected (Gold, etc.).
            # Set a cooldown to stop retrying — the agent should sell ingots instead.
            mat_fails = ctx.blackboard.get("_craft_bs_material_fails", 0) + 1
            ctx.blackboard["_craft_bs_material_fails"] = mat_fails

            # CircuitBreaker: only count when we know we had enough ingots
            # (that's the material-type mismatch signal)
            cb = ctx.blackboard.get("_craft_material_breaker")
            if cb is not None and ingots_before >= ingot_cost:
                cb.record_failure("iron")
                if cb.is_open("iron"):
                    logger.warning(
                        "craft_bs_material_cooldown",
                        fails=cb.failure_count("iron"),
                        ingots=ingots_before,
                        cost=ingot_cost,
                    )

            # Legacy flag fallback (kept for tests that don't install breaker)
            if mat_fails >= 3 and ingots_before >= ingot_cost:
                cooldown = min(300 * (mat_fails - 2), 1800)  # escalate: 300→600→…→1800s
                ctx.blackboard["_craft_bs_material_cooldown"] = time.time() + cooldown
                logger.warning(
                    "craft_bs_material_cooldown",
                    fails=mat_fails,
                    ingots=ingots_before,
                    cost=ingot_cost,
                    cooldown_sec=cooldown,
                )
            logger.warning(
                "craft_blacksmith_no_material",
                item=item_name, notice=last_notice,
                ingots_counted=ingots_before, ingot_cost=ingot_cost,
                material_fails=mat_fails,
            )
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message=f"Server says insufficient metal for {item_name} "
                        f"(counted {ingots_before}, need {ingot_cost}) — {last_notice}",
            )

        if (stop_reason == "no_station" or "anvil" in notice_lower
                or "forge" in notice_lower):
            logger.warning("craft_blacksmith_no_station", notice=last_notice)
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message=f"No anvil/forge — {last_notice}",
            )

        # Nothing changed and no recognizable result — unclear
        fail_count = ctx.blackboard.get("_craft_bs_fails", 0) + 1
        ctx.blackboard["_craft_bs_fails"] = fail_count

        logger.warning(
            "craft_blacksmith_unclear",
            item=item_name, fail_count=fail_count, stop=stop_reason,
            notice=last_notice or "(none)",
            ingots_before=ingots_before, ingots_after=ingots_after,
            items_before=items_before, items_after=items_after,
        )

        if fail_count >= 5:
            ctx.blackboard["_craft_bs_fails"] = 0
            return ProcedureResult(
                success=False,
                reason=FailureReason.PERMANENT,
                message=f"Gave up crafting after {fail_count} unclear results"
                        f" — last notice: {last_notice or 'none'}",
            )

        return ProcedureResult(
            success=False,
            reason=FailureReason.BLOCKED,
            message=f"Craft result unclear ({fail_count}/5)"
                    f" — notice: {last_notice or 'none'}",
        )

    async def _open_gump_and_craft(
        self, ctx: AgentContext, grp_idx: int, item_idx: int,
    ) -> ProcedureResult | None:
        """Full craft-gump navigation: open the tongs gump, force Iron,
        click the group button, then the item's make button.

        Returns a failure ProcedureResult, or None when the craft attempt
        was sent (result pending — ServUO re-sends the gump on resolution).
        """
        ss = ctx.perception.self_state
        tools = find_in_backpack(ctx, TONGS_GRAPHICS)
        if not tools:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no tongs",
            )

        # 1. Open blacksmithy gump
        ss.gumps.clear()
        await ctx.conn.send_packet(build_double_click(tools[0].serial))
        await asyncio.sleep(0.5)

        result = await wait_for_gump(ctx, timeout=3.0)
        if not result.success:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="blacksmithy gump did not open",
            )

        gump = result.data["gump"]

        # 1b. Force Iron material to avoid stale CraftContext selecting Gold etc.
        #     GetButtonID(6, 0) = 7 opens resource selection page
        #     GetButtonID(5, 0) = 6 selects Iron (resource index 0)
        material_btn = _get_button_id(6, 0)  # 7
        iron_btn = _get_button_id(5, 0)      # 6
        if gump.find_button_by_id(material_btn):
            logger.info("craft_bs_material_page", button_id=material_btn)
            ss.gumps.clear()
            switches = [sw.switch_id for sw in gump.switches if sw.initial_state]
            text_entries = [
                (te.entry_id, te.initial_text) for te in gump.text_entries
            ]
            await ctx.conn.send_packet(build_gump_response(
                serial=gump.serial, gump_id=gump.gump_id,
                button_id=material_btn,
                switches=switches, text_entries=text_entries,
            ))
            await asyncio.sleep(0.5)

            result = await wait_for_gump(ctx, timeout=3.0)
            if not result.success:
                await self._close_all_gumps(ctx)
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.BLOCKED,
                    message="resource selection gump did not appear",
                )
            gump = result.data["gump"]

            if gump.find_button_by_id(iron_btn):
                logger.info("craft_bs_select_iron", button_id=iron_btn)
                ss.gumps.clear()
                switches = [
                    sw.switch_id for sw in gump.switches if sw.initial_state
                ]
                text_entries = [
                    (te.entry_id, te.initial_text) for te in gump.text_entries
                ]
                await ctx.conn.send_packet(build_gump_response(
                    serial=gump.serial, gump_id=gump.gump_id,
                    button_id=iron_btn,
                    switches=switches, text_entries=text_entries,
                ))
                await asyncio.sleep(0.5)

                result = await wait_for_gump(ctx, timeout=3.0)
                if not result.success:
                    await self._close_all_gumps(ctx)
                    return ProcedureResult(
                        success=False,
                        reason=FailureReason.BLOCKED,
                        message="gump did not refresh after selecting Iron",
                    )
                gump = result.data["gump"]
            else:
                # Iron button not found — material cannot be set to Iron.
                # Proceeding would craft with wrong material (Gold, etc.)
                available = [b.button_id for b in gump.reply_buttons()]
                logger.warning(
                    "craft_bs_iron_btn_missing",
                    expected=iron_btn,
                    available_buttons=available[:20],
                )
                await self._close_all_gumps(ctx)
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.BLOCKED,
                    message=f"Iron button {iron_btn} not found on resource page "
                            f"— cannot force Iron material (available: {available[:10]})",
                )
        else:
            # Material page button not found — cannot verify/force Iron.
            # Proceeding risks crafting with a stale material (Gold, etc.)
            # which causes "insufficient metal" loops.
            logger.warning(
                "craft_bs_no_material_btn",
                expected=material_btn,
                buttons=[b.button_id for b in gump.reply_buttons()][:20],
            )
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"Material page button {material_btn} not found in gump "
                        f"— cannot force Iron material",
            )

        # 2. Click category using computed ServUO button ID
        cat_btn_id = _get_button_id(0, grp_idx)
        if not gump.find_button_by_id(cat_btn_id):
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"category button {cat_btn_id} (group {grp_idx}) not in gump",
            )

        ss.gumps.clear()
        switches = [sw.switch_id for sw in gump.switches if sw.initial_state]
        text_entries = [(te.entry_id, te.initial_text) for te in gump.text_entries]
        await ctx.conn.send_packet(build_gump_response(
            serial=gump.serial, gump_id=gump.gump_id,
            button_id=cat_btn_id,
            switches=switches,
            text_entries=text_entries,
        ))
        await asyncio.sleep(0.5)

        # 3. Wait for item list gump
        result = await wait_for_gump(ctx, timeout=3.0)
        if not result.success:
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="item list gump did not appear",
            )

        gump = result.data["gump"]

        # 4. Click specific item using computed ServUO button ID
        create_btn_id = _get_button_id(1, item_idx)
        if not gump.find_button_by_id(create_btn_id):
            await self._close_all_gumps(ctx)
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"item button {create_btn_id} (index {item_idx}) not in gump",
            )

        ss.gumps.clear()
        switches = [sw.switch_id for sw in gump.switches if sw.initial_state]
        text_entries = [(te.entry_id, te.initial_text) for te in gump.text_entries]
        await ctx.conn.send_packet(build_gump_response(
            serial=gump.serial, gump_id=gump.gump_id,
            button_id=create_btn_id,
            switches=switches,
            text_entries=text_entries,
        ))

        # Craft attempt sent — the result arrives via the re-sent CraftGump
        # notice (or a journal line when the tool broke).
        return None
