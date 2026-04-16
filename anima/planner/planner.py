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

NOTE: `ctx.blackboard` is in the process of migrating to the typed
PlannerBlackboard in anima/planner/state.py. New code should prefer
`PlannerBlackboard.from_dict(ctx.blackboard)` and write back via
`bb.to_dict()` at the end of the operation. Existing string-key access
is still supported during the migration.
"""

from __future__ import annotations

import asyncio
import json
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from anima.procedures.base import (
    FailureReason,
    Procedure,
    ProcedureRegistry,
    ProcedureResult,
    _record_action_log,
)
from anima.planner.circuit_breaker import CircuitBreaker
from anima.planner.deadlock import DeadlockResolver
from anima.planner.expedition import (
    BATCH_CRAFT_INGOTS,
    BATCH_SMELT_ORE,
    MiningExpedition,
    Phase,
)
from anima.planner.health import PlannerHealth
from anima.planner.roaming import RoamingHelper
from anima.planner.strategy import StrategySelector
from anima.planner.goals import GoalStack
from anima.planner.helpers import (
    _MoveToProcedure,
    _PickUpAndSmelt,
    _ScavengeGroundItems,
)
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
        self._backpack_refresh_fails: int = 0  # consecutive failed backpack refreshes
        # Idle / stuck loop detection
        self._idle_ticks: int = 0           # consecutive ticks with no procedure
        self._repeat_counter: dict[str, int] = {}  # procedure → consecutive fail count
        self._last_procedure: str = ""
        self._last_escalation: float = 0.0  # last time we escalated a deadlock
        # Fast loop detection — planner self-monitors to break infinite
        # selection loops without waiting for the supervisor analysis cycle.
        self._health = PlannerHealth(window=30, min_diversity=0.2)
        self._health_break_until: float = 0.0
        # 1 failure in an 8×8 ServUO ore bank = the whole bank is
        # depleted for the 10-20 min server respawn window.
        self._bank_breaker = CircuitBreaker(max_failures=1, cooldown_s=600.0)
        # After 3 "insufficient metal" failures with enough ingots on hand
        # (material-type mismatch), cool down the material for 5 minutes.
        self._craft_material_breaker = CircuitBreaker(
            max_failures=3, cooldown_s=300.0,
        )
        # 2 server-refused pickups on the same ore serial → mark as junk
        # so _find_ground_ore skips it. Long cooldown (1h) because the
        # server-side reason for refusal is usually LOS/z/anti-cheat and
        # unlikely to change on its own.
        self._ore_pickup_breaker = CircuitBreaker(
            max_failures=2, cooldown_s=3600.0,
        )
        self._deadlock = DeadlockResolver(self)
        self._roaming = RoamingHelper(self)
        self._strategy = StrategySelector(interval_s=300.0)
        self._goals = GoalStack()
        self._expedition = MiningExpedition()

    def stop(self) -> None:
        self._running = False

    async def run(self, ctx: AgentContext) -> None:
        """Main planner loop. Runs until connection drops."""
        self._running = True
        logger.info("planner_started")

        # Expose breakers on the blackboard so skills/procedures can reach them
        ctx.blackboard["_bank_breaker"] = self._bank_breaker
        ctx.blackboard["_craft_material_breaker"] = self._craft_material_breaker
        ctx.blackboard["_ore_pickup_breaker"] = self._ore_pickup_breaker

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
                # Refresh high-level strategy (LLM-driven, ~5 min interval)
                try:
                    await self._strategy.maybe_refresh(ctx)
                except Exception as e:
                    logger.warning("strategy_refresh_error", error=str(e))

                # Update goal stack — pop satisfied / expired goals
                try:
                    self._goals.update(ctx)
                except Exception as e:
                    logger.warning("goal_update_error", error=str(e))

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

                # --- Planner health / loop detection ---
                proc_name_for_health = (
                    getattr(result, '_proc_name', '') if result else ''
                ) or self._last_procedure
                if proc_name_for_health:
                    self._health.record(proc_name_for_health)

                if (self._health.is_looping()
                        and _time.time() > self._health_break_until):
                    # When the expedition is actively cycling (non-IDLE),
                    # mono-procedure runs are expected: mine_ore dominates
                    # in MINING, smelt_ore in COLLECTING, craft_blacksmith
                    # in CRAFTING_TRIP. The expedition's own 10-min watchdog
                    # detects actual stalls; the 60s health break would just
                    # interrupt productive work. Suppress it.
                    expedition = ctx.blackboard.get("expedition")
                    if (expedition is not None
                            and getattr(expedition.phase, "value", "idle") != "idle"):
                        self._health.reset()
                    else:
                        logger.warning(
                            "planner_health_loop_detected",
                            dominant=self._health.dominant_procedure(),
                            snapshot=self._health.snapshot(),
                        )
                        # Pause selection for 60 seconds and clear per-tick
                        # counters. Also reset _idle_ticks so _check_stuck()'s
                        # escalation timer doesn't race the health break —
                        # the break IS the recovery mechanism for this condition.
                        self._health_break_until = _time.time() + 60.0
                        self._health.reset()
                        self.continuation_hint = None
                        self._repeat_counter.clear()
                        self._idle_ticks = 0

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
        # Planner health break — after a loop was detected we pause
        # selection briefly so the environment has a chance to change.
        if _time.time() < self._health_break_until:
            return None

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

        _proc_start = _time.monotonic()
        result = await proc.run(ctx)
        ctx.blackboard["current_procedure"] = None

        # Helper procedures (ad-hoc classes like _MoveToProcedure,
        # _PickUpAndSmelt, etc.) don't subclass Procedure and therefore
        # don't auto-write action_logs. Supervisor reads action_logs to
        # detect idle planner loops, so writing an entry here keeps long
        # helper-driven tours (e.g. COLLECTING pile visits) visible as
        # liveness.
        if result is not None and not isinstance(proc, Procedure):
            ss = ctx.perception.self_state
            await _record_action_log(
                memory_db=ctx.memory_db,
                agent_name=ctx.persona.name if ctx.persona else "unknown",
                procedure=proc.name,
                location_x=ss.x,
                location_y=ss.y,
                result_str="success" if result.success else (
                    result.reason.value if result.reason else "failure"
                ),
                message=result.message or "",
                duration_ms=(_time.monotonic() - _proc_start) * 1000,
                details=result.details,
            )

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

        ctx.blackboard["expedition"] = self._expedition
        # Prune piles that have likely decayed server-side.
        self._expedition.prune_stale_piles()

        # Watchdog: if stuck in a non-IDLE phase for >10 minutes, reset.
        if (self._expedition.phase != Phase.IDLE
                and self._expedition.watchdog_expired(max_phase_s=600.0)):
            stuck_s = time.time() - self._expedition.phase_started_at  # capture before transition_to resets phase_started_at
            logger.warning(
                "expedition_watchdog",
                phase=self._expedition.phase.value,
                stuck_s=stuck_s,
            )
            self._expedition.piles.clear()
            self._expedition.transition_to(Phase.IDLE)

        # Skip procedures flagged by supervisor or repeat-failure blackboard
        skip_bb = ctx.blackboard.get("_skip_procedures", set())

        def _get_proc(name: str):
            if name in skip_bb or _is_supervisor_skipped(name):
                logger.info("planner_skipping", procedure=name,
                            reason="supervisor hint" if name not in skip_bb
                            else "repeat failure")
                return None
            if self._strategy.is_excluded(name):
                logger.debug(
                    "planner_strategy_skipping",
                    procedure=name,
                    strategy=self._strategy.current.name,
                )
                return None
            if self._goals.is_forbidden(name):
                logger.debug(
                    "planner_goal_forbidding",
                    procedure=name,
                    goal=self._goals.active.name if self._goals.active else None,
                )
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

            # Try to buy tools if we have gold — purchase works server-side
            # even without backpack detection; the arriving items should
            # trigger backpack discovery on the next equipment update.
            if ss.gold >= 10:
                proc = self.registry.get("buy_from_vendor")
                if proc and await proc.can_start(ctx):
                    ctx.blackboard["planner_intent"] = (
                        f"배낭 미감지, 금화 {ss.gold}g → 상점에서 도구 구매"
                    )
                    return proc
                move = await self._roaming.move_to_location(ctx, "tinker")
                if move:
                    ctx.blackboard["planner_intent"] = (
                        f"배낭 미감지, 금화 {ss.gold}g → 상점으로 이동"
                    )
                    return move

            if time.time() > self._move_fail_until:
                move_proc = await self._roaming.try_move_to_activity(ctx)
                if move_proc:
                    return move_proc

            logger.debug("planner_no_backpack")
            return None

        # --- Backpack content refresh ---
        # Backpack serial is known but may have stale/empty contents.
        # If weight is significant but no items are visible in the backpack,
        # double-click it to trigger server to resend container contents (0x3C).
        bp_items = sum(
            1 for it in ctx.perception.world.items.values()
            if it.container == backpack
        )
        if bp_items == 0 and ss.weight > 50:
            now = _time.time()
            refresh_interval = 15.0 if self._backpack_refresh_fails < 8 else 120.0
            if now - self._last_backpack_request > refresh_interval:
                self._last_backpack_request = now
                self._backpack_refresh_fails += 1
                from anima.client.packets import build_double_click

                if self._backpack_refresh_fails >= 4:
                    # Serial is likely stale — re-request equipment from server
                    logger.warning(
                        "planner_backpack_stale_serial",
                        serial=hex(backpack),
                        attempts=self._backpack_refresh_fails,
                    )
                    player_serial = ss.serial
                    await ctx.conn.send_packet(build_double_click(player_serial))
                    await asyncio.sleep(1.5)
                    new_bp = ss.equipment.get(0x15)
                    if new_bp and new_bp != backpack:
                        logger.info(
                            "planner_backpack_redetected",
                            old=hex(backpack), new=hex(new_bp),
                        )
                        backpack = new_bp
                    await ctx.conn.send_packet(build_double_click(backpack))
                    await asyncio.sleep(1.0)
                    bp_items = sum(
                        1 for it in ctx.perception.world.items.values()
                        if it.container == backpack
                    )
                    if bp_items > 0:
                        self._backpack_refresh_fails = 0
                else:
                    logger.info(
                        "planner_refreshing_backpack",
                        backpack=hex(backpack),
                        weight=f"{ss.weight}/{ss.weight_max}",
                        attempt=self._backpack_refresh_fails,
                    )
                    await ctx.conn.send_packet(build_double_click(backpack))
                    await asyncio.sleep(0.5)
        elif bp_items > 0:
            self._backpack_refresh_fails = 0

        from anima.skills.crafting.tinker import TINKER_TOOLS_GRAPHICS

        SHOVEL_GRAPHICS = {0x0F39}

        # --- Inventory snapshot ---
        mining_tools = find_in_backpack(ctx, PICKAXE_GRAPHICS | SHOVEL_GRAPHICS)
        has_mining_tool = bool(mining_tools)
        mining_tool_count = len(mining_tools)
        has_tinker_tools = bool(find_in_backpack(ctx, TINKER_TOOLS_GRAPHICS))
        from anima.procedures.craft_blacksmith import TONGS_GRAPHICS
        has_tongs = bool(find_in_backpack(ctx, TONGS_GRAPHICS))
        ore_count = count_items(ctx, ORE_GRAPHICS)
        # Exclude ore hues proven unsmelable at current skill level,
        # and iron ore piles too small to smelt (amount < 2)
        unsmelable_ore_hues = ctx.blackboard.get("_unsmelable_ore_hues", set())
        small_iron_serials = ctx.blackboard.get("_small_iron_ore_serials", set())
        if unsmelable_ore_hues or small_iron_serials:
            smeltable_ore = sum(
                it.amount for it in ctx.perception.world.items.values()
                if it.container == backpack and it.graphic in ORE_GRAPHICS
                and it.hue not in unsmelable_ore_hues
                and not (it.serial in small_iron_serials and it.amount < 2)
            )
        else:
            smeltable_ore = ore_count
        # Count IRON ingots only (hue 0) — colored ingots are not usable for basic recipes
        from anima.procedures.craft_blacksmith import _count_iron_ingots
        ingot_count = _count_iron_ingots(ctx)
        # Material cooldown = iron forcing failed repeatedly → craft will fail, sell instead.
        # Prefer the CircuitBreaker (single source of truth with can_start);
        # fall back to the legacy timestamp flag for contexts/tests that don't
        # install the breaker.
        craft_material_blocked = self._craft_material_breaker.is_open("iron") or (
            time.time() < ctx.blackboard.get("_craft_bs_material_cooldown", 0)
        )

        from anima.procedures.craft_blacksmith import CRAFTED_ITEM_GRAPHICS
        crafted_count = sum(
            1 for it in ctx.perception.world.items.values()
            if it.container == backpack and it.graphic in CRAFTED_ITEM_GRAPHICS
        )
        from anima.procedures.bank_deposit import _has_colored_ingots
        has_colored_ingots = _has_colored_ingots(ctx)

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

        # --- Priority 2: Overweight → smelt (only if carrying enough ore) ---
        # Server requires ≥2 ore per pile to produce an ingot; smelting 1
        # always fails with "not enough metal", creating a mine→smelt loop.
        if ss.weight_max > 0 and ss.weight > ss.weight_max * 0.85:
            if smeltable_ore >= 2:
                proc = _get_proc("smelt_ore")
                if proc and await proc.can_start(ctx):
                    _intent(f"과적 ({ss.weight}/{ss.weight_max}) → 광석 제련")
                    return proc
                # Has smeltable ore but no forge nearby — go to forge
                _intent(f"과적 ({ss.weight}/{ss.weight_max}) → 용광로로 이동")
                return await self._roaming.move_to_location(ctx, "forge", "blacksmith")
            # Overweight from non-ore items (crafted items, etc.) — fall
            # through to sell/bank priorities below

        # --- MINING → COLLECTING transition ---
        expedition = self._expedition
        if expedition.should_start_collecting(
            scan_empty=not self._scan_has_mineable_bank(ctx),
        ):
            expedition.transition_to(Phase.COLLECTING)

        # --- Priority 3: Batch smelt (only in COLLECTING) ---
        if expedition.phase == Phase.COLLECTING and smeltable_ore >= BATCH_SMELT_ORE:
            proc = _get_proc("smelt_ore")
            if proc and await proc.can_start(ctx):
                _intent(f"수거 단계, 광석 {smeltable_ore}개 → 일괄 제련")
                return proc
            _intent(f"수거 단계, 광석 {smeltable_ore}개, 용광로 필요 → 이동")
            return await self._roaming.move_to_location(ctx, "forge", "blacksmith")

        # --- Priority 3b: Collection tour — pick up next pile ---
        if expedition.phase == Phase.COLLECTING:
            can_carry_more = ss.weight_max == 0 or ss.weight <= ss.weight_max - 50
            if (can_carry_more
                    and self.continuation_hint != "smelt_ore"
                    and "pick_up_ore_and_smelt" not in skip_bb):
                ground_ore = self._find_ground_ore(ctx, ss)
                if expedition.piles or (ground_ore and sum(it.amount for it in ground_ore) >= 2):
                    _intent(
                        f"수거 투어: 더미 {len(expedition.piles)}개 → 다음 더미 줍기"
                    )
                    return _PickUpAndSmelt(ground_ore, ss)

        # --- COLLECTING → CRAFTING_TRIP or COLLECTING → MINING ---
        # Before exiting COLLECTING, also check for untracked ground ore
        # (dropped before this expedition cycle started, e.g. from a
        # previous session or before agent restart). If any exists nearby,
        # stay in COLLECTING so _PickUpAndSmelt's Path B fallback can
        # pick it up — otherwise the agent mines MORE ore while piles of
        # earlier ore rot on the ground un-smelted.
        if expedition.phase == Phase.COLLECTING and not expedition.piles:
            ground_ore = self._find_ground_ore(ctx, ss)
            ground_ore_total = sum(it.amount for it in ground_ore) if ground_ore else 0
            if ground_ore and ground_ore_total >= 2:
                _intent(
                    f"수거 중, 바닥에 미수거 광석 {ground_ore_total}개 → 계속 수거"
                )
                return _PickUpAndSmelt(ground_ore, ss)

            weight_ratio = (ss.weight / ss.weight_max) if ss.weight_max > 0 else 0.0
            if expedition.should_leave_mine(
                ingot_count=ingot_count,
                weight_ratio=weight_ratio,
                has_pickaxe=has_mining_tool,
            ):
                expedition.transition_to(Phase.CRAFTING_TRIP)
            else:
                expedition.transition_to(Phase.MINING)

        # --- Priority 4: No mining tools → get them ---
        if not has_mining_tool:
            # 4a: Has ore → smelt first
            if ore_count > 0:
                proc = _get_proc("smelt_ore")
                if proc and await proc.can_start(ctx):
                    _intent("곡괭이 없음, 광석 보유 → 제련부터")
                    return proc
                _intent("곡괭이 없음, 광석 보유, 용광로 없음 → 용광로로 이동")
                return await self._roaming.move_to_location(ctx, "forge", "blacksmith")

            # 4b: Has tinker tools + ingots → try craft tools
            #     Skip if Tinkering gave up (skill too low)
            if (has_tinker_tools and ingot_count >= 4
                    and not ctx.blackboard.get("_make_tools_gave_up")):
                proc = _get_proc("make_tools")
                if proc and await proc.can_start(ctx):
                    _intent(f"곡괭이 없음, 주석도구+주괴 {ingot_count}개 → 도구 제작")
                    return proc

            # 4c: Buy tools from a tinker (pickaxe costs ~11 gold).
            # This server deducts vendor purchases from BANK GOLD, not
            # backpack. We cache the last reading in `bank_balance`:
            #   - fresh + sufficient → proceed to buy_from_vendor
            #   - fresh + insufficient → disable buy, fall through
            #   - stale/missing + banker nearby → check_bank_balance first
            #   - stale/missing + no banker → blind try (old behavior);
            #     buy_from_vendor will self-disable on failure.
            buy_disabled_until = ctx.blackboard.get("_buy_disabled_until", 0)
            if ss.gold >= 10 and time.time() >= buy_disabled_until:
                PICKAXE_COST = 11
                bal_cache = ctx.blackboard.get("bank_balance") or {}
                bal_amount = bal_cache.get("amount")
                bal_ts = bal_cache.get("ts", 0)
                bal_fresh = (
                    bal_amount is not None
                    and (time.time() - bal_ts) < 600
                )

                if bal_fresh and bal_amount < PICKAXE_COST:
                    # Known insufficient → skip buy, block for 10 min
                    ctx.blackboard["_buy_disabled_until"] = time.time() + 600
                    _intent(
                        f"은행 잔액 {bal_amount}gp 부족 → 구매 건너뛰고 다른 경로 시도"
                    )
                    # Fall through to 4d/4e/4f below
                elif bal_fresh:
                    # Known sufficient → buy
                    proc = _get_proc("buy_from_vendor")
                    if proc and await proc.can_start(ctx):
                        _intent(
                            f"곡괭이 없음, 은행 {bal_amount}gp → 상점에서 구매"
                        )
                        return proc
                    _intent(f"곡괭이 없음, 은행 {bal_amount}gp → 상점으로 이동")
                    move = await self._roaming.move_to_location(
                        ctx, "tinker", "provisioner", "miner",
                    )
                    if move:
                        return move
                else:
                    # Unknown balance. Prefer reading it from a banker.
                    cbb = _get_proc("check_bank_balance")
                    if cbb and await cbb.can_start(ctx):
                        _intent("곡괭이 없음 → 은행 잔액 먼저 확인")
                        return cbb
                    # No banker reachable right now → blind buy attempt;
                    # buy_from_vendor self-disables on failure.
                    proc = _get_proc("buy_from_vendor")
                    if proc and await proc.can_start(ctx):
                        _intent(
                            f"곡괭이 없음, 금화 {ss.gold}g (은행 미확인) → 상점에서 구매"
                        )
                        return proc
                    _intent(
                        f"곡괭이 없음, 금화 {ss.gold}g → 은행/상점으로 이동"
                    )
                    move = await self._roaming.move_to_location(
                        ctx, "bank", "tinker", "provisioner", "miner",
                    )
                    if move:
                        return move

            # 4d: Has ingots + tongs → craft weapons to sell for gold to buy tools
            #     Skip when material cooldown is active (iron forcing failed)
            if ingot_count >= 8 and has_tongs and not craft_material_blocked:
                proc = _get_proc("craft_blacksmith")
                if proc and await proc.can_start(ctx):
                    _intent(f"곡괭이 없음, 주괴 {ingot_count}개 → 무기 제작 후 판매하여 자금 마련")
                    logger.info("planner_craft_for_gold", reason="need tools, crafting to sell")
                    return proc
                _intent(f"곡괭이 없음, 주괴 {ingot_count}개 → 대장간으로 이동")
                move = await self._roaming.move_to_location(ctx, "forge", "blacksmith")
                if move:
                    return move

            # 4e: Has ingots but can't craft → sell raw ingots
            if ingot_count > 0:
                proc = _get_proc("sell_to_vendor")
                if proc and await proc.can_start(ctx):
                    _intent(f"곡괭이 없음, 주괴 {ingot_count}개 → 주괴 판매")
                    return proc
                _intent(f"곡괭이 없음, 주괴 {ingot_count}개 → 상점으로 이동")
                move = await self._roaming.move_to_location(ctx, "blacksmith", "weaponsmith")
                if move:
                    return move

            # NEW STOP: explicit standstill when no recovery path exists.
            # If buy was just disabled (bank gold likely empty) AND we have
            # no ingots to make_tools / craft / sell, then deadlock recovery
            # (random walk, scavenge, monster hunt) is futile — the agent
            # would need user intervention (drop ingots, refill bank) to
            # progress. Surface the standstill on stdio and yield this tick
            # so the loop visibility is maintained without burning cycles.
            buy_disabled = time.time() < ctx.blackboard.get("_buy_disabled_until", 0)
            if buy_disabled and ingot_count == 0 and ore_count == 0:
                _intent(
                    "정지: 도구 없음, 잉갓 없음, 구매 불가 — 사용자 개입 필요"
                )
                logger.warning(
                    "planner_no_progress_path",
                    reason="no tool, no ingots, buy disabled — user must intervene",
                )
                if ctx.bus is not None:
                    try:
                        ctx.bus.publish("planner.stopped", {
                            "message": "✗ 정지: 도구 없음, 잉갓 없음, 구매 불가",
                            "importance": 3,
                        })
                    except Exception:
                        pass
                return None

            # 4f: TRUE DEADLOCK — no tools, no gold, no ore, no ingots
            # Progressive recovery:
            #   Level 0: scavenge nearby + walk to town (3 attempts)
            #   Level 1: drop junk + walk to town
            #   Level 2: wander-explore (random walk scanning for items)
            #   Level 3: hunt monsters for gold
            #   Level 4: forum escalation, then reset cycle
            logger.info("planner_deadlock_recovery_attempt")

            _deadlock_attempts = ctx.blackboard.get("_deadlock_attempt_count", 0)
            _deadlock_level = ctx.blackboard.get("_deadlock_recovery_level", 0)

            # Always count visits — this drives escalation even when
            # nothing is found (unlike the old scavenge-only counter).
            ctx.blackboard["_deadlock_attempt_count"] = _deadlock_attempts + 1

            # After 3 attempts at current level, escalate to next.
            if _deadlock_attempts >= 3:
                _deadlock_level += 1
                ctx.blackboard["_deadlock_recovery_level"] = _deadlock_level
                ctx.blackboard["_deadlock_attempt_count"] = 0
                logger.warning(
                    "planner_deadlock_escalating",
                    attempts=_deadlock_attempts,
                    new_level=_deadlock_level,
                    weight=f"{ss.weight}/{ss.weight_max}",
                )

                # Level 1: Drop junk + walk to town
                if _deadlock_level == 1:
                    if ss.weight_max > 0 and ss.weight > ss.weight_max * 0.5:
                        _intent("교착 복구 Lv1: 불필요 아이템 버리기")
                        return _DropJunkItems(ss)
                    if time.time() > self._move_fail_until:
                        move = await self._roaming.move_to_location(
                            ctx, "bank", "tavern", "inn", "blacksmith",
                        )
                        if move:
                            _intent("교착 복구 Lv1: 마을로 이동하여 자원 탐색")
                            return move

                # Level 2: Wander-explore — random walk around town
                elif _deadlock_level == 2:
                    _intent("교착 복구 Lv2: 마을 주변 탐색 (랜덤 이동)")
                    return _WanderAndScavenge(ss)

                # Level 3: Hunt monsters for gold
                elif _deadlock_level == 3:
                    target = self._find_huntable_target(ctx, ss)
                    if target:
                        _intent(f"교착 복구 Lv3: {target.name or 'monster'} 사냥")
                        return _HuntForGold(target, ss)
                    # No targets in town — move to wilderness where monsters spawn
                    if time.time() > self._move_fail_until:
                        move = await self._roaming.move_to_location(
                            ctx, "mine", "mining", "camp",
                        )
                        if move:
                            _intent("교착 복구 Lv3: 몬스터 없음 → 야외로 이동")
                            return move

                # Level 4+: Forum escalation, then reset cycle
                if _deadlock_level >= 4:
                    ctx.blackboard["_deadlock_recovery_level"] = 0
                    _intent("교착 상태 Lv4: 포럼에 도움 요청")
                    await self._deadlock.escalate_to_forum(ctx)
                    return None

            # Try to find valuable items on the ground nearby
            ground_items = self._deadlock.find_ground_valuables(ctx, ss)
            if ground_items:
                _intent(f"교착 복구: 바닥에 아이템 {len(ground_items)}개 발견 → 줍기")
                return _ScavengeGroundItems(ground_items, ss)

            # At hunting level, try to find and attack nearby monsters
            if _deadlock_level >= 3:
                target = self._find_huntable_target(ctx, ss)
                if target:
                    _intent(f"교착 복구 Lv3: {target.name or 'monster'} 사냥")
                    return _HuntForGold(target, ss)

            # Walk toward useful area — at hunting level, go to wilderness
            # where monsters spawn; otherwise try populated town areas.
            if time.time() > self._move_fail_until:
                if _deadlock_level >= 3:
                    _intent("교착 복구: 사냥감 탐색 → 야외로 이동")
                    move = await self._roaming.move_to_location(
                        ctx, "mine", "mining", "camp",
                    )
                else:
                    _intent("교착 복구: 주변에 아이템 없음 → 마을로 이동")
                    move = await self._roaming.move_to_location(
                        ctx, "bank", "tavern", "inn", "blacksmith",
                    )
                if move:
                    return move

            # All immediate paths exhausted — return None; attempt counter
            # was already incremented so we'll escalate after 3 idle entries.
            _intent("교착 상태: 복구 시도 중 → 다음 레벨로 에스컬레이션 대기")
            return None

        # Made it past Priority 4 (have mining tools) → reset deadlock state
        ctx.blackboard.pop("_deadlock_recovery_level", None)
        ctx.blackboard.pop("_deadlock_attempt_count", None)

        # --- Priority 4.5: Tool restock (non-blocking) ---
        # Agent has at least 1 tool (passed Priority 4) but fewer than
        # TOOL_MIN_STOCK. Buy more if a vendor is reachable right now;
        # otherwise fall through and keep mining — this is opportunistic,
        # not a hard block.
        from anima.procedures.buy_from_vendor import TOOL_MIN_STOCK
        if mining_tool_count < TOOL_MIN_STOCK and ss.gold >= 10:
            proc = _get_proc("buy_from_vendor")
            if proc and await proc.can_start(ctx):
                _intent(
                    f"도구 보충 ({mining_tool_count}/{TOOL_MIN_STOCK}) → 상점 구매"
                )
                return proc

        # --- Priority 5: Batch craft — accumulate ingots first ---
        # Gated by phase to avoid crafting mid-collection tour, but allowed
        # in MINING too so ingots never pile up indefinitely when the
        # COLLECTING trigger does not fire (e.g. in dense mining areas
        # where banks always respawn before MINING exits).
        if ingot_count >= BATCH_CRAFT_INGOTS and expedition.phase != Phase.COLLECTING:
            if has_tongs and not craft_material_blocked:
                proc = _get_proc("craft_blacksmith")
                if proc and await proc.can_start(ctx):
                    _intent(f"주괴 {ingot_count}개 보유 → 무기/방어구 제작")
                    return proc
                # Has tongs but no forge/anvil — go to blacksmith
                _intent(f"주괴 {ingot_count}개 보유, 대장간 필요 → 대장간으로 이동")
                move = await self._roaming.move_to_location(ctx, "forge", "blacksmith")
                if move:
                    return move
                # Can't reach any forge — fall through to sell logic below

            # Strategy: ALWAYS prefer crafting over selling raw ingots.
            # Crafted weapons sell for more gold than raw ingots, and
            # crafting trains Blacksmithing.

            # Material blocked (cooldown after "insufficient metal")
            # → don't sell raw, mine more while cooldown expires.
            if craft_material_blocked:
                logger.debug("planner_craft_material_cooldown_waiting")
                pass  # fall through to Priority 7 (mine)

            # No tongs → get tongs first, then craft
            elif not has_tongs:
                if ss.gold >= 10:
                    proc = _get_proc("buy_from_vendor")
                    if proc and await proc.can_start(ctx):
                        _intent(f"집게 없음, 금화 {ss.gold}g → 집게 구매")
                        return proc
                    _intent(f"집게 없음, 금화 {ss.gold}g → 팅커 상점으로 이동")
                    move = await self._roaming.move_to_location(ctx, "tinker")
                    if move:
                        return move
                # No tongs AND no gold — last resort: sell just enough
                # raw ingots to fund a tongs purchase (~15g).
                else:
                    proc = _get_proc("sell_to_vendor")
                    if proc and await proc.can_start(ctx):
                        _intent(
                            f"집게 없음, 금화 없음 → 주괴 일부 판매하여 집게 구매 자금 마련"
                        )
                        return proc

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
            move = await self._roaming.move_to_location(ctx, *vendor_kw)
            if move:
                logger.info("planner_sell_crafted", items=crafted_count, vendor_keywords=vendor_kw)
                _intent(f"제작품 {crafted_count}개 보유 → {', '.join(vendor_kw)} 상점으로 이동")
                return move
            # No vendor reachable — fall through to lower priorities
            logger.info("planner_sell_no_vendor", items=crafted_count, vendor_keywords=vendor_kw)

        # Priority 5c removed: raw ingot selling is no longer a general
        # strategy. Ingots are reserved for crafting. The only raw-sell
        # path is the "no tongs + no gold" last resort in Priority 5.

        # --- CRAFTING_TRIP → MINING ---
        if expedition.phase == Phase.CRAFTING_TRIP and expedition.home_base is not None:
            near_home = max(
                abs(ss.x - expedition.home_base[0]),
                abs(ss.y - expedition.home_base[1]),
            ) <= 30
            if expedition.should_return_to_mine(
                ingot_count=ingot_count,
                crafted_count=crafted_count,
                near_home=near_home,
            ):
                expedition.cycles_completed += 1
                duration = _time.time() - expedition.phase_started_at
                logger.info(
                    "expedition_cycle_complete",
                    cycles=expedition.cycles_completed,
                    duration_s=duration,
                )
                if ctx.bus is not None:
                    try:
                        ctx.bus.publish("expedition.cycle_complete", {
                            "message": f"✓ 원정 사이클 {expedition.cycles_completed}회 완료 ({duration:.0f}s)",
                            "importance": 3,
                        })
                    except Exception:
                        pass  # activity publish must never break the planner
                expedition.transition_to(Phase.MINING)

        # --- Priority 5d: No tongs + no gold + colored ingots → sell for tongs ---
        # Colored ingots can't be used for basic crafting, so normally they get
        # banked at Priority 6.  But if we need gold to buy tongs, sell them
        # instead — otherwise the agent loops mine→smelt→bank forever.
        if not has_tongs and ss.gold < 10 and has_colored_ingots:
            proc = _get_proc("sell_to_vendor")
            if proc and await proc.can_start(ctx):
                _intent("집게 없음, 금화 없음 → 색상 주괴 판매하여 자금 마련")
                return proc
            vendor_kw = get_vendor_keywords_for_items(set(INGOT_GRAPHICS))
            move = await self._roaming.move_to_location(ctx, *vendor_kw)
            if move:
                _intent(f"집게 없음 → {', '.join(vendor_kw)} 상점으로 이동")
                return move

        # --- Priority 5e: Tinkering training ---
        # When Tinkering is below target and we have spare ingots + tinker
        # tools, craft extras to raise the skill. Runs after blacksmith/sell
        # paths so the primary Blacksmith training gets first dibs on ingots;
        # tinker training picks up whatever's left.
        from anima.procedures.make_tools import (
            TINKERING_TRAIN_TARGET as _TINK_TARGET,
            TINKERING_SKILL_ID as _TINK_ID,
        )
        _tinker_skill = next(
            (s.value for s in ss.skills.values() if s.id == _TINK_ID), 0.0
        )
        if (_tinker_skill < _TINK_TARGET
                and 4 <= ingot_count < BATCH_CRAFT_INGOTS
                and has_tinker_tools
                and not ctx.blackboard.get("_make_tools_gave_up")
                and _time.time() >= ctx.blackboard.get("_tinkering_blocked_until", 0)
                and ss.weight_max > 0
                and ss.weight < ss.weight_max * 0.85):
            proc = _get_proc("make_tools")
            if proc and await proc.can_start(ctx):
                _intent(
                    f"Tinkering 훈련 ({_tinker_skill:.0f}/{_TINK_TARGET:.0f}) → 도구 제작"
                )
                return proc

        # --- Priority 6: Gold > 200 OR colored ingots → bank ---
        # Colored (non-iron) ingots can't be used for our basic crafting
        # recipes, so they get banked rather than left in the backpack.
        if ss.gold > 200 or has_colored_ingots:
            proc = _get_proc("bank_deposit")
            if proc and await proc.can_start(ctx):
                if has_colored_ingots:
                    _intent("색상 주괴 보유 → 은행에 보관")
                else:
                    _intent(f"금화 {ss.gold}g 보유 → 은행에 예금")
                return proc
            if has_colored_ingots:
                _intent("색상 주괴 보유 → 은행으로 이동")
            else:
                _intent(f"금화 {ss.gold}g 보유 → 은행으로 이동")
            return await self._roaming.move_to_location(ctx, "bank")

        # --- Mining exhaustion guard ---
        # When all veins were depleted (10 consecutive failures), skip mining
        # for 5 min while the server regenerates resources.
        _mine_exhausted = time.time() < ctx.blackboard.get(
            "_mine_exhausted_until", 0
        )

        # --- Priority 7: Has mining tool → mine ---
        if has_mining_tool:
            if ctx.blackboard.pop("_had_no_mining_tool", False):
                ctx.blackboard.pop("_make_tools_gave_up", None)
        else:
            ctx.blackboard["_had_no_mining_tool"] = True
        mine_proc = _get_proc("mine_ore")
        if not _mine_exhausted:
            if mine_proc and await mine_proc.can_start(ctx):
                _intent("광산 근처, 곡괭이 보유 → 채광 시작")
                return mine_proc

            # --- Priority 7b: Near mine but not close enough → walk to ore ---
            # Only navigate toward mineable tiles when mine_ore is actually
            # available — if it was excluded by strategy/goals/skip_bb,
            # walking there creates a pointless movement loop.
            if mine_proc is not None and has_mining_tool and time.time() > self._move_fail_until:
                from anima.skills.gathering.mine import (
                    SEARCH_RADIUS as _MINE_SEARCH_R,
                    _find_mineable_tile,
                )
                _blocked = {k for k, v in self._failed_destinations.items()
                            if time.time() - v < 300.0}
                tile = _find_mineable_tile(ctx, blocked=_blocked)
                if tile is not None:
                    tx, ty = tile[0], tile[1]
                    dist = max(abs(tx - ss.x), abs(ty - ss.y))
                    if dist > _MINE_SEARCH_R:
                        _intent(f"채광 타일 발견 ({tx},{ty}), 거리 {dist} → 접근 이동")
                        return _MoveToProcedure(
                            f"mineable tile ({tx},{ty})", tx, ty,
                        )
                else:
                    # No mineable tile in MOVE_RADIUS — every nearby bank
                    # is depleted. Mark the closest mine LOCATION as
                    # exhausted so Priority 9 picks a different one.
                    self._roaming.mark_nearby_mine_exhausted(ctx, ss)

        # --- Priority 7c: Mining blocked → smelt available ore ---
        # When mining is blocked (depleted banks, voluntary cooldown, or
        # exhaustion guard), use the downtime to smelt any accumulated ore
        # rather than sitting idle for 2+ minutes.
        if ore_count > 0:
            proc = _get_proc("smelt_ore")
            if proc and await proc.can_start(ctx):
                _intent(f"채광 불가, 광석 {ore_count}개 → 대기 중 제련")
                return proc
            if smeltable_ore >= 2:
                move = await self._roaming.move_to_location(ctx, "forge", "blacksmith")
                if move:
                    _intent(f"채광 불가, 광석 {smeltable_ore}개 → 용광로로 이동")
                    return move

        # --- Priority 8: Continuation hint ---
        if self.continuation_hint:
            proc = _get_proc(self.continuation_hint)
            if proc and await proc.can_start(ctx):
                _intent(f"이전 작업 계속 → {self.continuation_hint}")
                return proc
            self.continuation_hint = None

        # --- Priority 9: Move to mine ---
        # Skip mine navigation when mine_ore is excluded (strategy/goals) —
        # otherwise the agent ping-pongs between mine locations with nothing
        # to do there.
        if mine_proc is not None and not _mine_exhausted and time.time() > self._move_fail_until:
            _intent("할 일 없음 → 광산으로 이동")
            move_proc = await self._roaming.try_move_to_activity(ctx)
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

        # If a PlannerHealth break is in progress the planner is *supposed*
        # to be idle — don't let idle-tick escalation fire during the break
        # (it would race the health break and reset its own counters).
        if _time.time() < self._health_break_until:
            return

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
            await self._deadlock.resolve(ctx)

        elif self._idle_ticks == self._IDLE_FORUM:
            await self._deadlock.escalate_to_forum(ctx)

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
                # mine_ore exhaustion: all veins depleted, server regen is
                # 10-20 min — set a 5-minute cooldown so the agent does other
                # activities instead of bouncing between depleted mines.
                if proc_name == "mine_ore":
                    ctx.blackboard["_mine_exhausted_until"] = _time.time() + 300
                    logger.info("planner_mine_exhausted", cooldown_sec=300)
                ctx.blackboard["planner_intent"] = (
                    f"{proc_name} {count}회 연속 실패 → 일시 스킵"
                )
                if ctx.bus:
                    ctx.bus.publish("system.stuck", {
                        "message": f"{proc_name} failed {count}x consecutively — skipping",
                        "importance": 2,
                    })

    def _scan_has_mineable_bank(self, ctx) -> bool:
        """True if at least one un-depleted mineable bank is within MOVE_RADIUS."""
        from anima.skills.gathering.mine import _find_mineable_tile
        return _find_mineable_tile(ctx) is not None

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
                    and max(abs(it.x - ss.x), abs(it.y - ss.y)) <= 2):
                result.append(it)
        return result

    def _find_huntable_target(self, ctx: AgentContext, ss):
        """Find a nearby monster the agent can attack for gold loot."""
        from anima.perception.enums import NotorietyFlag

        ATTACKABLE = {
            NotorietyFlag.ATTACKABLE,
            NotorietyFlag.CRIMINAL,
            NotorietyFlag.ENEMY,
            NotorietyFlag.MURDERER,
        }
        HUMAN_BODIES = {0x0190, 0x0191}

        no_gold_bodies = ctx.blackboard.get("_hunt_no_gold_bodies", set())

        candidates = []
        for m in ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=18):
            if m.serial == ss.serial:
                continue
            if m.notoriety not in ATTACKABLE:
                continue
            # Don't attack humans unless clearly hostile
            if m.body in HUMAN_BODIES and m.notoriety == NotorietyFlag.ATTACKABLE:
                continue
            # Skip creature types we've killed before that dropped no gold
            if m.body in no_gold_bodies:
                continue
            candidates.append(m)

        if not candidates:
            return None

        # Prefer closest target
        candidates.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))
        return candidates[0]

class _DropJunkItems:
    """Deadlock recovery: drop non-essential items to free weight.

    When the agent is stuck in a deadlock and overweight from scavenged
    items that don't break the deadlock (not tools/ore/ingots), drop them
    to free capacity for picking up actually useful items later.
    """

    def __init__(self, ss) -> None:
        self.name = "drop_junk_items"
        self.description = "Drop non-essential items to free weight"
        self._player_pos = (ss.x, ss.y)

    async def can_start(self, ctx) -> bool:
        return True

    async def run(self, ctx) -> ProcedureResult:
        import asyncio

        from anima.client.packets import build_drop_item
        from anima.procedures.craft_blacksmith import TONGS_GRAPHICS
        from anima.skills.crafting.smelt import INGOT_GRAPHICS
        from anima.skills.crafting.tinker import TINKER_TOOLS_GRAPHICS
        from anima.skills.gathering.mine import ORE_GRAPHICS, PICKAXE_GRAPHICS

        ss = ctx.perception.self_state
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no backpack",
            )

        GOLD_GRAPHIC = 0x0EED
        SHOVEL_GRAPHICS = {0x0F39}
        BANDAGE_GRAPHIC = 0x0E21

        # Keep items useful for the mining/crafting loop
        keep = (
            ORE_GRAPHICS
            | INGOT_GRAPHICS
            | PICKAXE_GRAPHICS
            | SHOVEL_GRAPHICS
            | TINKER_TOOLS_GRAPHICS
            | TONGS_GRAPHICS
            | {GOLD_GRAPHIC, BANDAGE_GRAPHIC}
        )

        droppable = [
            it for it in ctx.perception.world.items.values()
            if it.container == backpack and it.graphic not in keep
        ]

        if not droppable:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no droppable items in backpack",
            )

        dropped = 0
        for item in droppable[:10]:
            # Drop to ground at current position (container=0 → ground)
            await ctx.conn.send_packet(
                build_drop_item(item.serial, x=ss.x, y=ss.y, z=ss.z)
            )
            await asyncio.sleep(0.4)
            dropped += 1
            logger.info(
                "dropped_junk_item",
                serial=f"0x{item.serial:08X}",
                graphic=f"0x{item.graphic:04X}",
                amount=item.amount,
            )

        return ProcedureResult(
            success=dropped > 0,
            message=f"Dropped {dropped} junk items to free weight",
            reason=None if dropped > 0 else FailureReason.MISSING_RESOURCE,
        )


class _WanderAndScavenge:
    """Deadlock recovery Lv2: wander around town scanning for useful items.

    Takes random steps through the town area.  At each stop, checks for
    valuable ground items within pickup range and grabs them.  This covers
    areas the agent hasn't visited yet, unlike the stationary scavenge.
    """

    WANDER_STEPS = 20
    STEP_DELAY = 0.6  # seconds between wander steps

    def __init__(self, ss) -> None:
        self.name = "wander_and_scavenge"
        self.description = "Wander around town scanning for items"
        self._start_pos = (ss.x, ss.y)

    async def can_start(self, ctx) -> bool:
        return True

    async def run(self, ctx) -> ProcedureResult:
        import asyncio
        import random

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
        spots_visited = 0

        for _ in range(self.WANDER_STEPS):
            # Pick a random nearby position (±5..15 tiles) and walk there
            ss = ctx.perception.self_state
            dx = random.choice([-1, 1]) * random.randint(5, 15)
            dy = random.choice([-1, 1]) * random.randint(5, 15)
            target_x = ss.x + dx
            target_y = ss.y + dy

            await go_to(ctx, target_x, target_y)
            spots_visited += 1

            # Scan for valuable items at new position
            ss = ctx.perception.self_state
            items = self._scan_valuables(ctx, ss)
            for item in items[:3]:
                if ss.weight_max > 0 and ss.weight > ss.weight_max - 50:
                    break
                dist = max(abs(item.x - ss.x), abs(item.y - ss.y))
                if dist > 2:
                    arrived = await go_to(ctx, item.x, item.y)
                    if not arrived:
                        continue
                await ctx.conn.send_packet(build_pick_up(item.serial, item.amount))
                await asyncio.sleep(0.3)
                await ctx.conn.send_packet(
                    build_drop_item(item.serial, container=backpack)
                )
                await asyncio.sleep(0.3)
                picked += 1
                logger.info(
                    "wander_scavenged",
                    serial=f"0x{item.serial:08X}",
                    graphic=f"0x{item.graphic:04X}",
                    spot=spots_visited,
                )

            # Early exit if we found enough useful items
            if picked >= 5:
                break

        if picked > 0:
            return ProcedureResult(
                success=True,
                message=f"Wandered {spots_visited} spots, scavenged {picked} items",
            )
        return ProcedureResult(
            success=False,
            reason=FailureReason.MISSING_RESOURCE,
            message=f"Wandered {spots_visited} spots, found nothing useful",
        )

    @staticmethod
    def _scan_valuables(ctx, ss) -> list:
        """Find valuable ground items within pickup range of current pos."""
        from anima.procedures.craft_blacksmith import TONGS_GRAPHICS
        from anima.skills.crafting.smelt import INGOT_GRAPHICS
        from anima.skills.crafting.tinker import TINKER_TOOLS_GRAPHICS
        from anima.skills.gathering.mine import ORE_GRAPHICS, PICKAXE_GRAPHICS

        GOLD_GRAPHIC = 0x0EED
        SHOVEL_GRAPHICS = {0x0F39}

        valuable = (
            ORE_GRAPHICS | INGOT_GRAPHICS | PICKAXE_GRAPHICS
            | SHOVEL_GRAPHICS | TINKER_TOOLS_GRAPHICS | TONGS_GRAPHICS
            | {GOLD_GRAPHIC}
        )

        result = []
        for it in ctx.perception.world.items.values():
            if (it.container == 0
                    and it.graphic in valuable
                    and max(abs(it.x - ss.x), abs(it.y - ss.y)) <= 2):
                result.append(it)
        result.sort(key=lambda it: max(abs(it.x - ss.x), abs(it.y - ss.y)))
        return result


class _HuntForGold:
    """Deadlock recovery Lv3: attack a nearby monster and loot gold.

    Walks to the target, enters war mode, fights until target dies or
    timeout, then picks up any gold dropped on the ground.
    """

    COMBAT_TIMEOUT = 30.0
    COMBAT_TICK = 1.0

    def __init__(self, target, ss) -> None:
        self.name = "hunt_for_gold"
        self.description = f"Hunt {target.name or 'monster'} for gold"
        self._target_serial = target.serial
        self._target_name = target.name or "monster"
        self._target_pos = (target.x, target.y)
        self._target_body = target.body

    async def can_start(self, ctx) -> bool:
        return True

    async def run(self, ctx) -> ProcedureResult:
        import asyncio
        import time

        from anima.action.movement import go_to
        from anima.client.packets import (
            build_attack,
            build_drop_item,
            build_pick_up,
            build_war_mode,
        )

        ss = ctx.perception.self_state

        # Walk toward target if not adjacent
        dist = max(abs(self._target_pos[0] - ss.x),
                    abs(self._target_pos[1] - ss.y))
        if dist > 1:
            arrived = await go_to(ctx, self._target_pos[0], self._target_pos[1])
            if not arrived:
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.BLOCKED,
                    message=f"Cannot reach {self._target_name}",
                )

        # Check target still exists
        mob = ctx.perception.world.mobiles.get(self._target_serial)
        if mob is None:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message=f"{self._target_name} is gone",
            )

        # Enter war mode and attack
        await ctx.conn.send_packet(build_war_mode(True))
        await asyncio.sleep(0.3)
        await ctx.conn.send_packet(build_attack(self._target_serial))
        logger.info("hunt_attack_start", target=self._target_name)

        # Combat loop
        deadline = time.monotonic() + self.COMBAT_TIMEOUT
        target_killed = False
        chase_failures = 0
        MAX_CHASE_FAILURES = 3

        while time.monotonic() < deadline:
            await asyncio.sleep(self.COMBAT_TICK)

            # Bail if HP too low
            ss = ctx.perception.self_state
            if ss.hits_max > 0 and ss.hits < ss.hits_max * 0.2:
                logger.warning("hunt_retreat", hp=ss.hits)
                break

            mob = ctx.perception.world.mobiles.get(self._target_serial)
            if mob is None:
                target_killed = True
                break

            # Chase target if it moved away
            dist = max(abs(mob.x - ss.x), abs(mob.y - ss.y))
            if dist > 1:
                logger.info("hunt_chasing", target=self._target_name, dist=dist)
                arrived = await go_to(ctx, mob.x, mob.y)
                if not arrived:
                    chase_failures += 1
                    if chase_failures >= MAX_CHASE_FAILURES:
                        logger.warning("hunt_chase_gave_up",
                                       target=self._target_name,
                                       failures=chase_failures)
                        break
                    continue  # not adjacent — skip attack this tick

                # Re-check distance; mob may have moved during go_to
                ss = ctx.perception.self_state
                mob = ctx.perception.world.mobiles.get(self._target_serial)
                if mob is None:
                    target_killed = True
                    break
                dist = max(abs(mob.x - ss.x), abs(mob.y - ss.y))
                if dist > 1:
                    chase_failures += 1
                    if chase_failures >= MAX_CHASE_FAILURES:
                        logger.warning("hunt_chase_gave_up",
                                       target=self._target_name,
                                       failures=chase_failures)
                        break
                    continue  # still not adjacent after chase

            # Re-send attack (only reached when adjacent)
            await ctx.conn.send_packet(build_attack(self._target_serial))

        # Exit war mode
        await ctx.conn.send_packet(build_war_mode(False))
        await asyncio.sleep(0.3)

        if not target_killed:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"Could not kill {self._target_name}",
            )

        logger.info("hunt_killed", target=self._target_name)

        # Loot gold from the ground near the kill site
        ss = ctx.perception.self_state
        backpack = ss.equipment.get(0x15)
        gold_picked = 0
        GOLD_GRAPHIC = 0x0EED

        if backpack:
            await asyncio.sleep(1.0)  # wait for corpse/loot to appear
            for it in ctx.perception.world.items.values():
                if (it.container == 0
                        and it.graphic == GOLD_GRAPHIC
                        and max(abs(it.x - ss.x), abs(it.y - ss.y)) <= 2):
                    await ctx.conn.send_packet(build_pick_up(it.serial, it.amount))
                    await asyncio.sleep(0.3)
                    await ctx.conn.send_packet(
                        build_drop_item(it.serial, container=backpack)
                    )
                    await asyncio.sleep(0.3)
                    gold_picked += 1
                    logger.info("hunt_looted_gold", amount=it.amount)

        # Track bodies that never drop gold so we stop wasting time on them
        if gold_picked == 0 and self._target_body:
            no_gold = ctx.blackboard.setdefault("_hunt_no_gold_bodies", set())
            no_gold.add(self._target_body)
            logger.info("hunt_no_gold_body", body=hex(self._target_body),
                        name=self._target_name)

        return ProcedureResult(
            success=True,
            message=f"Killed {self._target_name}, looted {gold_picked} gold piles",
        )
