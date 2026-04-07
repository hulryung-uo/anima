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
import json
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from anima.procedures.base import FailureReason, ProcedureRegistry, ProcedureResult
from anima.web.command_bus import CommandBus

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

# Minimum delay between planner loops to prevent spin on rapid failures
MIN_LOOP_DELAY = 0.2

SUPERVISOR_HINTS_FILE = Path(__file__).parent.parent.parent / "data" / "supervisor_hints.json"


def _is_supervisor_skipped(procedure: str) -> bool:
    """Check if supervisor has flagged this procedure for skipping."""
    if not SUPERVISOR_HINTS_FILE.exists():
        return False
    try:
        hints = json.loads(SUPERVISOR_HINTS_FILE.read_text())
        skip = hints.get("skip_procedures", {})
        entry = skip.get(procedure)
        if entry and entry.get("until", 0) > _time.time():
            return True
    except Exception:
        pass
    return False


class Planner:
    """Selects and runs procedures based on priority rules."""

    def __init__(self, registry: ProcedureRegistry, command_bus: CommandBus | None = None) -> None:
        self.registry = registry
        self.command_bus = command_bus
        self.continuation_hint: str | None = None
        self._running = False
        self._last_trade_time: float = 0.0
        self._move_fail_until: float = 0.0  # cooldown after move-to failure
        self._failed_destinations: dict[tuple[int, int], float] = {}  # (x,y) → time
        self._last_backpack_request: float = 0.0  # cooldown for re-requesting equipment
        # Idle / stuck loop detection
        self._idle_ticks: int = 0           # consecutive ticks with no procedure
        self._repeat_counter: dict[str, int] = {}  # procedure → consecutive fail count
        self._last_procedure: str = ""
        self._last_escalation: float = 0.0  # last time we escalated a deadlock

    def stop(self) -> None:
        self._running = False

    async def run(self, ctx: AgentContext) -> None:
        """Main planner loop. Runs until connection drops."""
        self._running = True
        logger.info("planner_started")

        while self._running and ctx.conn.connected:
            # --- Request names for nearby unnamed NPCs ---
            # Without names, _find_vendor cannot identify NPC types
            ss = ctx.perception.self_state
            for mob in ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=18):
                if not mob.name and mob.serial != ss.serial:
                    from anima.client.packets import build_opl_request, build_single_click
                    await ctx.conn.send_packet(build_opl_request(mob.serial))
                    await ctx.conn.send_packet(build_single_click(mob.serial))

            # --- External steering ---
            if self.command_bus and self.command_bus.paused:
                await asyncio.sleep(0.5)
                continue

            try:
                # Check for override commands from dashboard
                override_result = await self._handle_overrides(ctx)
                if override_result is not None:
                    result = override_result
                else:
                    result = await self.tick(ctx)

                if result:
                    self.continuation_hint = result.next_suggestion
                    if not result.success and hasattr(result, 'message') and 'Could not reach' in (result.message or ''):
                        import time
                        self._move_fail_until = time.time() + 30.0
                        logger.info("planner_move_cooldown", seconds=30)
                    # --- Track repeat failures ---
                    self._idle_ticks = 0
                    proc_name = getattr(result, '_proc_name', '') or self._last_procedure
                    if not result.success:
                        self._repeat_counter[proc_name] = self._repeat_counter.get(proc_name, 0) + 1
                    else:
                        self._repeat_counter[proc_name] = 0
                else:
                    self.continuation_hint = None
                    self._idle_ticks += 1

                # --- Stuck / deadlock detection ---
                await self._check_stuck(ctx)

            except Exception as e:
                logger.error("planner_tick_error", error=str(e))

            await asyncio.sleep(MIN_LOOP_DELAY)

        logger.info("planner_stopped")

    async def _handle_overrides(self, ctx: AgentContext) -> ProcedureResult | None:
        """Check CommandBus for steering overrides. Returns result if handled."""
        if not self.command_bus:
            return None

        # Force go_to
        go_to = self.command_bus.override_go_to
        if go_to:
            x, y = go_to
            logger.info("planner_override_go_to", x=x, y=y)
            if ctx.bus:
                ctx.bus.publish("action.start", {
                    "message": f"→ Override: go to ({x}, {y})",
                    "importance": 2,
                })
            proc = _MoveToProcedure(f"override({x},{y})", x, y)
            return await proc.run(ctx)

        # Force procedure
        proc_name = self.command_bus.override_procedure
        if proc_name:
            proc = self.registry.get(proc_name)
            if proc:
                logger.info("planner_override_procedure", name=proc_name)
                if ctx.bus:
                    ctx.bus.publish("action.start", {
                        "message": f"▶ Override: {proc_name}",
                        "importance": 2,
                    })
                return await proc.run(ctx)
            else:
                logger.warning("planner_override_unknown", name=proc_name)

        return None

    async def tick(self, ctx: AgentContext) -> ProcedureResult | None:
        """One planner cycle: select procedure → run it → return result."""
        proc = await self.select_procedure(ctx)
        if proc is None:
            return None

        logger.info("planner_selected", procedure=proc.name)
        ctx.blackboard["current_procedure"] = proc.name
        self._last_procedure = proc.name

        # Publish activity to bus for TUI display
        if ctx.bus:
            ctx.bus.publish("action.start", {
                "message": f"▶ {proc.name}",
                "importance": 1,
            })

        result = await proc.run(ctx)
        ctx.blackboard["current_procedure"] = None

        # Track failed move destinations to prevent retry loops
        if result and not result.success and isinstance(proc, _MoveToProcedure):
            import time
            self._failed_destinations[(proc._x, proc._y)] = time.time()
            ctx.blackboard["planner_intent"] = (
                f"이동 실패: {proc.description} ({proc._x},{proc._y}) — "
                f"{result.message or '경로 없음'}"
            )

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

        # Skip procedures flagged by supervisor or repeat-failure blackboard
        skip_bb = ctx.blackboard.get("_skip_procedures", set())

        def _get_proc(name: str):
            if name in skip_bb or _is_supervisor_skipped(name):
                logger.info("planner_skipping", procedure=name,
                            reason="supervisor hint" if name not in skip_bb
                            else "repeat failure")
                return None
            return self.registry.get(name)

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
        from anima.procedures.craft_blacksmith import TONGS_GRAPHICS
        has_tongs = bool(find_in_backpack(ctx, TONGS_GRAPHICS))
        ore_count = count_items(ctx, ORE_GRAPHICS)
        # Exclude ore hues proven unsmelable at current skill level
        unsmelable_ore_hues = ctx.blackboard.get("_unsmelable_ore_hues", set())
        if unsmelable_ore_hues:
            smeltable_ore = sum(
                it.amount for it in ctx.perception.world.items.values()
                if it.container == backpack and it.graphic in ORE_GRAPHICS
                and it.hue not in unsmelable_ore_hues
            )
        else:
            smeltable_ore = ore_count
        # Count IRON ingots only (hue 0) — colored ingots are not usable for basic recipes
        from anima.procedures.craft_blacksmith import _count_iron_ingots
        ingot_count = _count_iron_ingots(ctx)
        # Material cooldown = iron forcing failed repeatedly → craft will fail, sell instead
        craft_material_blocked = time.time() < ctx.blackboard.get(
            "_craft_bs_material_cooldown", 0
        )

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
                tongs=has_tongs,
                ore=ore_count,
                ingot=ingot_count,
                crafted=crafted_count,
                gave_up_craft=bool(ctx.blackboard.get("_make_tools_gave_up")),
            )

        def _intent(text: str) -> None:
            ctx.blackboard["planner_intent"] = text

        # --- Priority 1: Survival ---
        if ss.hits_max > 0 and ss.hits < ss.hits_max * 0.3:
            proc = self.registry.get("heal_self")
            if proc and await proc.can_start(ctx):
                _intent(f"HP 위험 ({ss.hits}/{ss.hits_max}) → 치료")
                return proc

        # --- Priority 2: Overweight → smelt (only if carrying smeltable ore) ---
        if ss.weight_max > 0 and ss.weight > ss.weight_max * 0.85:
            if smeltable_ore > 0:
                proc = _get_proc("smelt_ore")
                if proc and await proc.can_start(ctx):
                    _intent(f"과적 ({ss.weight}/{ss.weight_max}) → 광석 제련")
                    return proc
                # Has smeltable ore but no forge nearby — go to forge
                _intent(f"과적 ({ss.weight}/{ss.weight_max}) → 용광로로 이동")
                return await self._move_to_location(ctx, "forge", "blacksmith")
            # Overweight from non-ore items (crafted items, etc.) — fall
            # through to sell/bank priorities below

        # --- Priority 3: Has smeltable ore → smelt ---
        if smeltable_ore > 0:
            proc = _get_proc("smelt_ore")
            if proc and await proc.can_start(ctx):
                _intent(f"광석 {smeltable_ore}개 보유 → 제련")
                return proc
            # Smeltable ore in backpack but no forge nearby — go to forge
            _intent(f"광석 {smeltable_ore}개 보유, 근처에 용광로 없음 → 용광로로 이동")
            return await self._move_to_location(ctx, "forge", "blacksmith")

        # --- Priority 3b: Ore on ground nearby → pick up then go smelt ---
        # Skip if too heavy to pick up anything (same 50-stone buffer as _PickUpAndSmelt)
        can_carry_more = ss.weight_max == 0 or ss.weight <= ss.weight_max - 50
        if can_carry_more:
            ground_ore = self._find_ground_ore(ctx, ss)
            if ground_ore:
                _intent(f"바닥에 광석 {len(ground_ore)}개 발견 → 줍기")
                return _PickUpAndSmelt(ground_ore, ss)

        # --- Priority 4: No mining tools → get them ---
        if not has_mining_tool:
            # 4a: Has ore → smelt first
            if ore_count > 0:
                proc = _get_proc("smelt_ore")
                if proc and await proc.can_start(ctx):
                    _intent("곡괭이 없음, 광석 보유 → 제련부터")
                    return proc
                _intent("곡괭이 없음, 광석 보유, 용광로 없음 → 용광로로 이동")
                return await self._move_to_location(ctx, "forge", "blacksmith")

            # 4b: Has tinker tools + ingots → try craft tools
            #     Skip if Tinkering gave up (skill too low)
            if (has_tinker_tools and ingot_count >= 4
                    and not ctx.blackboard.get("_make_tools_gave_up")):
                proc = _get_proc("make_tools")
                if proc and await proc.can_start(ctx):
                    _intent(f"곡괭이 없음, 주석도구+주괴 {ingot_count}개 → 도구 제작")
                    return proc

            # 4c: Has gold → buy tools directly (pickaxe costs ~11 gold)
            if ss.gold >= 10:
                proc = _get_proc("buy_from_vendor")
                if proc and await proc.can_start(ctx):
                    _intent(f"곡괭이 없음, 금화 {ss.gold}g → 상점에서 구매")
                    return proc
                _intent(f"곡괭이 없음, 금화 {ss.gold}g → 상점으로 이동")
                move = await self._move_to_location(ctx, "tinker", "provisioner")
                if move:
                    return move

            # 4d: Has ingots + tongs → craft weapons to sell for gold to buy tools
            #     Skip when material cooldown is active (iron forcing failed)
            if ingot_count >= 8 and not craft_material_blocked:
                proc = _get_proc("craft_blacksmith")
                if proc and await proc.can_start(ctx):
                    _intent(f"곡괭이 없음, 주괴 {ingot_count}개 → 무기 제작 후 판매하여 자금 마련")
                    logger.info("planner_craft_for_gold", reason="need tools, crafting to sell")
                    return proc
                _intent(f"곡괭이 없음, 주괴 {ingot_count}개 → 대장간으로 이동")
                move = await self._move_to_location(ctx, "forge", "blacksmith")
                if move:
                    return move

            # 4e: Has ingots but can't craft → sell raw ingots
            if ingot_count > 0:
                proc = _get_proc("sell_to_vendor")
                if proc and await proc.can_start(ctx):
                    _intent(f"곡괭이 없음, 주괴 {ingot_count}개 → 주괴 판매")
                    return proc
                _intent(f"곡괭이 없음, 주괴 {ingot_count}개 → 상점으로 이동")
                move = await self._move_to_location(ctx, "blacksmith", "weaponsmith")
                if move:
                    return move

            # 4f: TRUE DEADLOCK — no tools, no gold, no ore, no ingots
            # Recovery: scavenge ground items or walk to populated area
            logger.info("planner_deadlock_recovery_attempt")

            # Try to find valuable items on the ground nearby
            ground_items = self._find_ground_valuables(ctx, ss)
            if ground_items:
                _intent(f"교착 복구: 바닥에 아이템 {len(ground_items)}개 발견 → 줍기")
                return _ScavengeGroundItems(ground_items, ss)

            # Nothing on ground → walk to populated area (NOT mine)
            if time.time() > self._move_fail_until:
                _intent("교착 복구: 주변에 아이템 없음 → 마을로 이동")
                move = await self._move_to_location(
                    ctx, "bank", "tavern", "inn", "blacksmith",
                )
                if move:
                    return move

            # All recovery paths exhausted — return None, let _check_stuck
            # escalate to forum
            _intent("교착 상태: 복구 불가 → 도움 대기")
            return None

        # --- Priority 5: Has ingots → craft into weapons/armor ---
        if ingot_count >= 8:
            if has_tongs and not craft_material_blocked:
                proc = _get_proc("craft_blacksmith")
                if proc and await proc.can_start(ctx):
                    _intent(f"주괴 {ingot_count}개 보유 → 무기/방어구 제작")
                    return proc
                # Has tongs but no forge/anvil — go to blacksmith
                _intent(f"주괴 {ingot_count}개 보유, 대장간 필요 → 대장간으로 이동")
                move = await self._move_to_location(ctx, "forge", "blacksmith")
                if move:
                    return move
            else:
                # No tongs or material mismatch — can't craft
                # If no tongs and have gold → buy tongs from blacksmith vendor
                if not has_tongs and ss.gold >= 10:
                    proc = _get_proc("buy_from_vendor")
                    if proc and await proc.can_start(ctx):
                        _intent(f"집게 없음, 금화 {ss.gold}g → 집게 구매")
                        return proc
                    _intent(f"집게 없음, 금화 {ss.gold}g → 대장간 상점으로 이동")
                    move = await self._move_to_location(ctx, "blacksmith")
                    if move:
                        return move
                # Sell raw ingots (to get gold for tongs, or because material blocked)
                reason = "재료 불일치" if craft_material_blocked else "집게 없음"
                proc = _get_proc("sell_to_vendor")
                if proc and await proc.can_start(ctx):
                    _intent(f"{reason}, 주괴 {ingot_count}개 → 주괴 판매")
                    return proc
                from anima.procedures.vendor_knowledge import (
                    get_vendor_keywords_for_items,
                )
                vendor_kw = get_vendor_keywords_for_items(set(INGOT_GRAPHICS))
                move = await self._move_to_location(ctx, *vendor_kw)
                if move:
                    _intent(
                        f"{reason}, 주괴 {ingot_count}개 → "
                        f"{', '.join(vendor_kw)} 상점으로 이동"
                    )
                    return move

        # --- Priority 5b: Has crafted items → sell to appropriate vendor ---
        from anima.procedures.craft_blacksmith import CRAFTED_ITEM_GRAPHICS
        from anima.procedures.vendor_knowledge import get_vendor_keywords_for_items

        sell_graphics: set[int] = set()
        if backpack:
            for it in ctx.perception.world.items.values():
                if it.container == backpack and it.graphic in CRAFTED_ITEM_GRAPHICS:
                    sell_graphics.add(it.graphic)
                    crafted_count += 1

        if crafted_count > 0:
            proc = _get_proc("sell_to_vendor")
            if proc and await proc.can_start(ctx):
                _intent(f"제작품 {crafted_count}개 보유 → 상점에 판매")
                return proc
            # Find vendor that buys these specific items
            vendor_kw = get_vendor_keywords_for_items(sell_graphics)
            move = await self._move_to_location(ctx, *vendor_kw)
            if move:
                logger.info("planner_sell_crafted", items=crafted_count, vendor_keywords=vendor_kw)
                _intent(f"제작품 {crafted_count}개 보유 → {', '.join(vendor_kw)} 상점으로 이동")
                return move
            # No vendor reachable — fall through to lower priorities
            logger.info("planner_sell_no_vendor", items=crafted_count, vendor_keywords=vendor_kw)

        # --- Priority 5c: Has ingots but can't craft → sell raw ingots ---
        if ingot_count >= 10:
            proc = _get_proc("sell_to_vendor")
            if proc and await proc.can_start(ctx):
                _intent(f"주괴 {ingot_count}개 (제작 불가) → 주괴 판매")
                return proc
            vendor_kw = get_vendor_keywords_for_items(set(INGOT_GRAPHICS))
            move = await self._move_to_location(ctx, *vendor_kw)
            if move:
                _intent(f"주괴 {ingot_count}개 → {', '.join(vendor_kw)} 상점으로 이동")
                return move
            # No vendor reachable — fall through
            logger.info("planner_sell_ingots_no_vendor", ingots=ingot_count)

        # --- Priority 6: Gold > 200 → bank ---
        if ss.gold > 200:
            proc = _get_proc("bank_deposit")
            if proc and await proc.can_start(ctx):
                _intent(f"금화 {ss.gold}g 보유 → 은행에 예금")
                return proc
            _intent(f"금화 {ss.gold}g 보유 → 은행으로 이동")
            return await self._move_to_location(ctx, "bank")

        # --- Priority 7: Has mining tool → mine ---
        if has_mining_tool:
            # We have a tool again — reset gave-up flags so they can retry next time
            ctx.blackboard.pop("_make_tools_gave_up", None)
        proc = _get_proc("mine_ore")
        if proc and await proc.can_start(ctx):
            _intent("광산 근처, 곡괭이 보유 → 채광 시작")
            return proc

        # --- Priority 7b: Near mine but not close enough → walk to ore ---
        if has_mining_tool:
            from anima.skills.gathering.mine import (
                SEARCH_RADIUS as _MINE_SEARCH_R,
                _find_mineable_tile,
            )
            tile = _find_mineable_tile(ctx)
            if tile is not None:
                tx, ty = tile[0], tile[1]
                dist = max(abs(tx - ss.x), abs(ty - ss.y))
                if dist > _MINE_SEARCH_R:
                    _intent(f"채광 타일 발견 ({tx},{ty}), 거리 {dist} → 접근 이동")
                    return _MoveToProcedure(
                        f"mineable tile ({tx},{ty})", tx, ty,
                    )

        # --- Priority 8: Continuation hint ---
        if self.continuation_hint:
            proc = _get_proc(self.continuation_hint)
            if proc and await proc.can_start(ctx):
                _intent(f"이전 작업 계속 → {self.continuation_hint}")
                return proc
            self.continuation_hint = None

        # --- Priority 9: Move to mine ---
        if time.time() > self._move_fail_until:
            _intent("할 일 없음 → 광산으로 이동")
            move_proc = await self._try_move_to_activity(ctx)
            if move_proc:
                return move_proc

        _intent("대기 중")
        logger.debug("planner_no_procedure_available")
        return None

    # ------------------------------------------------------------------
    # Stuck / deadlock detection and resolution
    # ------------------------------------------------------------------

    # Thresholds (in ticks — 1 tick ≈ 200ms + procedure time)
    _IDLE_WARN = 50       # ~10s of no procedure → log warning
    _IDLE_ESCALATE = 150  # ~30s → try deadlock resolution
    _IDLE_FORUM = 600     # ~2min → post to forum for help + pause
    _REPEAT_FAIL_LIMIT = 10  # same procedure failing 10x → stop trying

    async def _check_stuck(self, ctx: AgentContext) -> None:
        """Detect stuck loops and deadlocks, escalate progressively."""
        import time as _time

        # --- Idle detection: planner returning None repeatedly ---
        if self._idle_ticks == self._IDLE_WARN:
            ss = ctx.perception.self_state
            logger.warning(
                "planner_idle_warning",
                idle_ticks=self._idle_ticks,
                pos=f"({ss.x},{ss.y})",
                gold=ss.gold,
                intent=ctx.blackboard.get("planner_intent", ""),
            )
            ctx.blackboard["planner_intent"] = "경고: 실행 가능한 작업 없음"
            if ctx.bus:
                ctx.bus.publish("system.stuck", {
                    "message": f"Planner idle {self._idle_ticks} ticks — no procedure available",
                    "importance": 2,
                })

        elif self._idle_ticks == self._IDLE_ESCALATE:
            await self._resolve_deadlock(ctx)

        elif self._idle_ticks == self._IDLE_FORUM:
            await self._escalate_to_forum(ctx)

        # --- Repeat failure: same procedure failing many times ---
        for proc_name, count in list(self._repeat_counter.items()):
            if count >= self._REPEAT_FAIL_LIMIT:
                logger.warning(
                    "planner_repeat_failure",
                    procedure=proc_name,
                    consecutive_fails=count,
                )
                # Temporarily skip this procedure
                skip = ctx.blackboard.setdefault("_skip_procedures", set())
                skip.add(proc_name)
                self._repeat_counter[proc_name] = 0
                ctx.blackboard["planner_intent"] = (
                    f"{proc_name} {count}회 연속 실패 → 일시 스킵"
                )
                if ctx.bus:
                    ctx.bus.publish("system.stuck", {
                        "message": f"{proc_name} failed {count}x consecutively — skipping",
                        "importance": 2,
                    })

    async def _resolve_deadlock(self, ctx: AgentContext) -> None:
        """Try to break out of a deadlock state."""
        import time as _time
        from anima.actions.inventory import find_in_backpack, count_items
        from anima.skills.gathering.mine import PICKAXE_GRAPHICS, ORE_GRAPHICS

        ss = ctx.perception.self_state
        has_pickaxe = bool(find_in_backpack(ctx, PICKAXE_GRAPHICS | {0x0F39}))
        ore = count_items(ctx, ORE_GRAPHICS)
        from anima.procedures.craft_blacksmith import _count_iron_ingots
        ingots = _count_iron_ingots(ctx)

        logger.warning(
            "planner_deadlock_analysis",
            pos=f"({ss.x},{ss.y})",
            gold=ss.gold,
            weight=f"{ss.weight}/{ss.weight_max}",
            has_pickaxe=has_pickaxe,
            ore=ore,
            ingots=ingots,
            failed_dests=len(self._failed_destinations),
            idle_ticks=self._idle_ticks,
        )

        ctx.blackboard["planner_intent"] = "교착 상태 분석 중..."

        # Strategy 1: Clear stale failed destinations (maybe they're available now)
        now = _time.time()
        cleared = 0
        for key in list(self._failed_destinations.keys()):
            if now - self._failed_destinations[key] > 120:  # 2min old → clear
                del self._failed_destinations[key]
                cleared += 1
        if cleared:
            logger.info("planner_cleared_failed_destinations", count=cleared)
            ctx.blackboard["planner_intent"] = f"실패 목적지 {cleared}개 초기화 → 재시도"
            self._idle_ticks = 0  # give the planner another chance
            return

        # Strategy 2: Clear depleted mines (maybe they've regenerated)
        depleted = ctx.blackboard.get("depleted_mines", {})
        old_depleted = [k for k, v in depleted.items() if now - v > 60]
        for k in old_depleted:
            del depleted[k]
        if old_depleted:
            logger.info("planner_cleared_depleted_mines", count=len(old_depleted))
            ctx.blackboard["planner_intent"] = f"고갈 광산 {len(old_depleted)}개 초기화 → 재시도"
            self._idle_ticks = 0
            return

        # Strategy 3: Clear refused vendors
        refused = ctx.blackboard.get("refused_vendors", {})
        if refused:
            refused.clear()
            logger.info("planner_cleared_refused_vendors")
            ctx.blackboard["planner_intent"] = "거부된 벤더 초기화 → 재시도"
            self._idle_ticks = 0
            return

        # Strategy 4: Clear skipped procedures
        skip = ctx.blackboard.get("_skip_procedures", set())
        if skip:
            skip.clear()
            ctx.blackboard.pop("_make_tools_gave_up", None)
            ctx.blackboard.pop("_craft_bs_fails", None)
            logger.info("planner_cleared_skip_procedures")
            ctx.blackboard["planner_intent"] = "스킵된 프로시저 초기화 → 재시도"
            self._idle_ticks = 0
            return

        # Strategy 5: True deadlock — no tools, no gold, no materials
        # Reset failed destinations so the agent can walk to new areas to scavenge
        if not has_pickaxe and ss.gold < 10 and ore == 0 and ingots == 0:
            logger.warning(
                "planner_true_deadlock_recovery",
                reason="no tools, no gold, no materials — clearing state for scavenge",
                pos=f"({ss.x},{ss.y})",
            )
            # Clear failed destinations so _move_to_location can find new targets
            self._failed_destinations.clear()
            self._move_fail_until = 0.0
            ctx.blackboard.pop("_skip_procedures", None)
            ctx.blackboard["planner_intent"] = (
                "교착 상태: 이동 제한 해제 → 마을에서 아이템 탐색"
            )
            if ctx.bus:
                ctx.bus.publish("system.deadlock", {
                    "message": "DEADLOCK: scavenging recovery — walking to town",
                    "importance": 3,
                })
            # Reset idle_ticks to give select_procedure another chance
            # with fresh state — Priority 4f will try scavenge/walk-to-town
            self._idle_ticks = 0
            return

    async def _escalate_to_forum(self, ctx: AgentContext) -> None:
        """Post a help request to forum and pause the planner."""
        import time as _time

        # Cooldown: don't spam forum (max once per 30 min)
        if _time.time() - self._last_escalation < 1800:
            return

        self._last_escalation = _time.time()
        ss = ctx.perception.self_state
        persona_name = ctx.persona.name if ctx.persona else "Anima"

        # Build help message
        has_pickaxe = False
        try:
            from anima.actions.inventory import find_in_backpack
            from anima.skills.gathering.mine import PICKAXE_GRAPHICS
            has_pickaxe = bool(find_in_backpack(ctx, PICKAXE_GRAPHICS | {0x0F39}))
        except Exception:
            pass

        situation = (
            f"위치: ({ss.x},{ss.y}), 금화: {ss.gold}, "
            f"무게: {ss.weight}/{ss.weight_max}, "
            f"곡괭이: {'있음' if has_pickaxe else '없음'}"
        )

        logger.warning("planner_forum_help_request", situation=situation)
        ctx.blackboard["planner_intent"] = "포럼에 도움 요청 게시 중..."

        # Try posting to forum
        if ctx.forum_client:
            try:
                title = f"{persona_name} — 도움이 필요합니다"
                body = (
                    f"안녕하세요, {persona_name}입니다.\n\n"
                    f"현재 어려운 상황에 처했습니다. {situation}\n\n"
                    f"도구와 금화가 모두 없어 작업을 계속할 수 없습니다. "
                    f"누군가 곡괭이나 약간의 금화를 도와주시면 감사하겠습니다.\n\n"
                    f"위치: ({ss.x}, {ss.y})에서 기다리고 있겠습니다."
                )
                post_id = await ctx.forum_client.create_post(title, body, "tavern")
                if post_id:
                    logger.info("planner_forum_help_posted", post_id=post_id)
                    if ctx.bus:
                        ctx.bus.publish("social.forum_post", {
                            "message": f"포럼에 도움 요청 게시: {title}",
                            "importance": 3,
                        })
            except Exception as e:
                logger.warning("planner_forum_help_failed", error=str(e))

        # Say something in-game too
        try:
            from anima.client.packets import build_unicode_speech
            await ctx.conn.send_packet(
                build_unicode_speech(f"I'm stuck and need help. No tools or gold. At ({ss.x},{ss.y})")
            )
        except Exception:
            pass

        # Pause and wait — maybe someone will help, or supervisor will intervene
        ctx.blackboard["planner_intent"] = "도움 대기 중 (5분간 일시 정지)"
        if ctx.bus:
            ctx.bus.publish("system.deadlock", {
                "message": "포럼에 도움 요청 완료. 5분간 대기 후 재시도.",
                "importance": 3,
            })

        # Wait 5 minutes, checking periodically if something changed
        for _ in range(30):  # 30 × 10s = 5min
            await asyncio.sleep(10.0)
            # Check if someone gave us tools or gold
            ss = ctx.perception.self_state
            if ss.gold >= 10:
                logger.info("planner_help_received", gold=ss.gold)
                ctx.blackboard["planner_intent"] = f"금화 {ss.gold}g 확보 → 재개"
                self._idle_ticks = 0
                return
            try:
                from anima.actions.inventory import find_in_backpack
                from anima.skills.gathering.mine import PICKAXE_GRAPHICS
                if find_in_backpack(ctx, PICKAXE_GRAPHICS | {0x0F39}):
                    logger.info("planner_help_received_tool")
                    ctx.blackboard["planner_intent"] = "곡괭이 확보 → 재개"
                    self._idle_ticks = 0
                    return
            except Exception:
                pass

        # After 5 min wait, reset and try again
        self._idle_ticks = 0
        self._failed_destinations.clear()
        ctx.blackboard.pop("depleted_mines", None)
        ctx.blackboard.pop("refused_vendors", None)
        ctx.blackboard.pop("_skip_procedures", None)
        ctx.blackboard.pop("_make_tools_gave_up", None)
        logger.info("planner_full_reset_after_wait")
        ctx.blackboard["planner_intent"] = "전체 초기화 후 재시도"

    def _find_ground_ore(self, ctx: AgentContext, ss) -> list:
        """Find ore items on the ground near the player (excluding junk and unsmelable hues)."""
        from anima.skills.gathering.mine import ORE_GRAPHICS
        junk = ctx.blackboard.get("_junk_ore_serials", set())
        unsmelable = ctx.blackboard.get("_unsmelable_ore_hues", set())
        result = []
        for it in ctx.perception.world.items.values():
            if (it.container == 0
                    and it.graphic in ORE_GRAPHICS
                    and it.serial not in junk
                    and it.hue not in unsmelable
                    and max(abs(it.x - ss.x), abs(it.y - ss.y)) <= 3):
                result.append(it)
        return result

    def _find_ground_valuables(self, ctx: AgentContext, ss) -> list:
        """Find ANY valuable items on the ground near the player.

        Used for deadlock recovery — scavenges gold, ore, ingots, tools,
        and crafted items within pickup range.
        """
        from anima.skills.gathering.mine import ORE_GRAPHICS, PICKAXE_GRAPHICS
        from anima.skills.crafting.smelt import INGOT_GRAPHICS
        from anima.skills.crafting.tinker import TINKER_TOOLS_GRAPHICS
        from anima.procedures.craft_blacksmith import TONGS_GRAPHICS

        GOLD_GRAPHIC = 0x0EED
        SHOVEL_GRAPHICS = {0x0F39}

        valuable = (
            ORE_GRAPHICS
            | INGOT_GRAPHICS
            | PICKAXE_GRAPHICS
            | SHOVEL_GRAPHICS
            | TINKER_TOOLS_GRAPHICS
            | TONGS_GRAPHICS
            | {GOLD_GRAPHIC}
        )

        junk = ctx.blackboard.get("_junk_ore_serials", set())
        result = []
        for it in ctx.perception.world.items.values():
            if (it.container == 0
                    and it.graphic in valuable
                    and it.serial not in junk
                    and max(abs(it.x - ss.x), abs(it.y - ss.y)) <= 18):
                result.append(it)

        # Sort by distance (closest first) for efficient pickup
        result.sort(key=lambda it: max(abs(it.x - ss.x), abs(it.y - ss.y)))
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
        import asyncio
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
