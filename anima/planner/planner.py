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
from anima.planner.meta_controller import MetaController
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

# Primary procedure chain per persona profession (persona.profession).
# Ordered: first startable procedure wins. Personas without a profession
# (or with an empty chain) fall through to the mining gameplay loop.
PROFESSION_LOOPS: dict[str, tuple[str, ...]] = {
    "mage": ("practice_magery",),
    # peacemaking first: it self-throttles on the 10s skill lockout and rolls
    # BOTH Musicianship and Peacemaking; plain plays fill the gaps.
    "bard": ("practice_peacemaking", "practice_music"),
    "thief": ("practice_hiding",),
    # hunt when a target is in range; otherwise roam to FIND one
    # (wander_for_combat) instead of falling through to the mining chain —
    # the largest COMBAT-uptime leak (variance-dominated seeds: engaged vs
    # stuck in mining fallthrough).
    # sell_to_vendor is last so looted weapons/armor get flushed to a
    # vendor once a backlog piles up (see the loot-flush gate in decide()):
    # without it the adventurer's worth_term stays structurally 0 —
    # bandage_self/hunt_nearby are first-startable-wins and never yield.
    "adventurer": (
        "bandage_self", "hunt_nearby", "wander_for_combat", "sell_to_vendor",
    ),
    # stationary, gump-resident craft loop (wiki optimum); smelt feeds it,
    # selling restocks. Falls through to the generic ladder when blocked.
    "blacksmith": ("craft_blacksmith", "smelt_ore", "sell_to_vendor"),
}

# Once this many vendor-sellable looted items (weapons/armor/etc. that a
# vendor will buy, excluding protected tools/materials) pile up in the pack,
# the adventurer loop prefers sell_to_vendor over hunting so the loot
# converts to gold (worth_term) instead of sitting dead in the backpack.
LOOT_SELL_BACKLOG_THRESHOLD = 6


def _count_sellable_loot_in_pack(ctx: AgentContext) -> int:
    """Count vendor-sellable looted items sitting in the backpack.

    "Sellable" = graphic appears in ITEM_VENDOR_MAP (some vendor buys it)
    and is NOT in KEEP_GRAPHICS (essential tools / raw materials / gold are
    never the sell backlog). Mirrors craft_blacksmith._count_crafted_in_pack
    but for the loot an adventurer accumulates from corpses.
    """
    from anima.procedures.vendor_knowledge import ITEM_VENDOR_MAP
    from anima.skills.trade.vendor import KEEP_GRAPHICS

    ss = ctx.perception.self_state
    backpack = ss.equipment.get(0x15)
    if not backpack:
        return 0
    return sum(
        getattr(item, "amount", 1)
        for item in ctx.perception.world.items.values()
        if item.container == backpack
        and item.graphic in ITEM_VENDOR_MAP
        and item.graphic not in KEEP_GRAPHICS
    )


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
        # Highest idle threshold already escalated in the current idle run.
        # _check_stuck() fires each escalation on a >= crossing (not ==) and
        # uses this watermark to fire each rung at most once per idle run, so
        # a counter that jumps past a threshold (health-break skip, a swallowed
        # tick exception) still escalates instead of silently sailing past it.
        self._idle_escalation_fired: int = 0
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
        # Meta-controller (docs/meta-controller.md). P0 SHADOW: runs and logs
        # which mode it WOULD pick, but does NOT drive selection — behaviour is
        # identical with it on or off. Promote to driving in P1.
        self._meta = MetaController(interval_s=180.0, shadow=True)
        self._goals = GoalStack()
        self._expedition = MiningExpedition()
        # Anti-freeze state (see tick() timeout + _liveness_watchdog).
        # Softer/earlier than _REPEAT_FAIL_LIMIT: demotes a failing
        # procedure for a cooldown instead of blacklisting it.
        self._proc_breaker = CircuitBreaker(
            max_failures=self._STARVE_FAILS, cooldown_s=self._STARVE_COOLDOWN_S,
        )
        self._current_proc_task: asyncio.Task | None = None
        self._watchdog_cancelled = False
        self._last_progress_ts: float = _time.monotonic()
        self._force_fallback_until: float = 0.0
        # Consecutive ticks the Priority-1b flee gate has chosen _FleeFromHostiles
        # while still wounded+swarmed. _FleeFromHostiles.run reports success even
        # on a no-op move, so the starvation breaker never demotes it; this
        # counter is the only thing that lets a trapped agent stop fleeing in
        # place and fall through to heal-in-place. Reset when no longer swarmed.
        self._flee_consecutive: int = 0

    # Anti-freeze knobs. A mutated procedure that blocks forever (awaiting
    # a packet that never comes) used to wedge the whole planner loop —
    # the liveness gate then crushes fitness (genome g_00004: 1 action in
    # 608s). Timeout caps a single execution; the watchdog catches stalls
    # earlier and forces fallback activity.
    _PROC_TIMEOUT_S = 300.0       # default per-procedure execution cap
    _WATCHDOG_POLL_S = 5.0        # watchdog sampling interval
    _WATCHDOG_STALL_S = 90.0      # no progress for this long → intervene
    _FALLBACK_WINDOW_S = 60.0     # forced-fallback selection window
    _STARVE_FAILS = 3             # consecutive failures → temp demotion
    _STARVE_COOLDOWN_S = 120.0    # demotion duration (auto-expires)

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

        # Anti-freeze watchdog runs as its own task so it can fire even
        # while tick() is blocked inside a stalled procedure.
        self._last_progress_ts = _time.monotonic()
        watchdog_task = asyncio.create_task(self._liveness_watchdog(ctx))
        try:
            await self._run_loop(ctx)
        finally:
            watchdog_task.cancel()

        logger.info("planner_stopped")

    async def _run_loop(self, ctx: AgentContext) -> None:
        while self._running and ctx.conn.connected:
            # --- Request names for nearby unnamed NPCs ---
            # Without names, _find_vendor cannot identify NPC types.
            # Guarded: a transient perception glitch or a bad packet build
            # here must not escape the loop and tear down the whole session
            # (the TaskGroup would cancel recv_loop and force a reconnect).
            # A real disconnect is handled by the `ctx.conn.connected` loop
            # condition, so swallowing here is safe.
            try:
                ss = ctx.perception.self_state
                for mob in ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=18):
                    if not mob.name and mob.serial != ss.serial:
                        from anima.client.packets import build_opl_request, build_single_click
                        await ctx.conn.send_packet(build_opl_request(mob.serial))
                        await ctx.conn.send_packet(build_single_click(mob.serial))
            except asyncio.CancelledError:
                raise  # real shutdown — propagate
            except Exception as e:
                logger.warning("planner_name_request_error", error=str(e))

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

                # Meta-controller (P0 SHADOW): log which mode it would pick.
                # Does not affect selection; spawns a background LLM task and
                # returns immediately (no-op when no LLM is configured).
                try:
                    await self._meta.maybe_decide(ctx)
                except Exception as e:
                    logger.warning("meta_decide_error", error=str(e))

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
                    if result.success:
                        # Progress signal for the anti-freeze watchdog
                        self._last_progress_ts = _time.monotonic()
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
                    dominant = self._health.dominant_procedure()
                    if (expedition is not None
                            and getattr(expedition.phase, "value", "idle") != "idle"):
                        self._health.reset()
                    elif self._is_productive_grind(dominant, result):
                        # PRODUCTIVE grind repeats by design (hunt_nearby landing
                        # kills, a practice loop gaining skill) — like an active
                        # expedition. The 60s break is only for a STUCK loop (the
                        # dominant procedure FAILING); a succeeding one (no
                        # consecutive failures) should keep working, not eat a
                        # 60s pause. Fixes the combat-uptime drain seen in soaks
                        # (30 productive hunt_nearby → false "looping" → 60s idle).
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
        # A forced-fallback window is the liveness watchdog's last line of
        # defense against a freeze, so it OUTRANKS the health break. The
        # watchdog only opens this window after _WATCHDOG_STALL_S of no
        # progress — and a 60s health break is itself a no-progress stall
        # the watchdog is meant to break out of. Checking the health break
        # first would swallow the forced fallback for the rest of the break,
        # re-freezing the very agent the watchdog just tried to free.
        force_fallback = _time.time() < self._force_fallback_until

        # Planner health break — after a loop was detected we pause
        # selection briefly so the environment has a chance to change.
        if not force_fallback and _time.time() < self._health_break_until:
            return None

        # During a forced-fallback window (set by the liveness watchdog)
        # prefer a guaranteed-liveness activity over normal selection.
        proc = None
        if force_fallback:
            proc = await self._fallback_procedure(ctx)
            if proc is not None:
                logger.info("planner_fallback_selected", procedure=proc.name)
        if proc is None:
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
        # Anti-freeze: cap execution time so a blocked procedure (e.g. a
        # mutation awaiting a server response that never comes) cannot
        # wedge the planner loop. The watchdog may also cancel the task.
        timeout_s = getattr(proc, "timeout_s", None) or self._PROC_TIMEOUT_S
        aborted: str | None = None
        self._watchdog_cancelled = False
        self._current_proc_task = asyncio.create_task(proc.run(ctx))
        try:
            result = await asyncio.wait_for(self._current_proc_task, timeout=timeout_s)
        except TimeoutError:
            aborted = "timeout"
            logger.warning(
                "procedure_timeout", procedure=proc.name, timeout_s=timeout_s,
            )
            result = ProcedureResult(
                success=False,
                reason=FailureReason.INTERRUPTED,
                message=f"anti-freeze timeout after {timeout_s:.0f}s",
            )
        except asyncio.CancelledError:
            if not self._watchdog_cancelled:
                raise  # real shutdown — propagate
            aborted = "watchdog_cancelled"
            logger.warning("procedure_watchdog_cancelled", procedure=proc.name)
            result = ProcedureResult(
                success=False,
                reason=FailureReason.INTERRUPTED,
                message="cancelled by liveness watchdog",
            )
        finally:
            self._current_proc_task = None
        ctx.blackboard["current_procedure"] = None

        # Aborted runs never reach Procedure.run()'s auto-logging —
        # record them here so supervisor/analysis can see freezes.
        if aborted is not None:
            ss = ctx.perception.self_state
            await _record_action_log(
                memory_db=ctx.memory_db,
                agent_name=ctx.persona.name if ctx.persona else "unknown",
                procedure=proc.name,
                location_x=ss.x,
                location_y=ss.y,
                result_str=aborted,
                message=result.message or "",
                duration_ms=(_time.monotonic() - _proc_start) * 1000,
                details=result.details,
            )

        # Starvation breaker: K consecutive failures demote the procedure
        # for a cooldown so lower-priority work gets a chance.
        if result.success:
            self._proc_breaker.record_success(proc.name)
        else:
            self._proc_breaker.record_failure(proc.name)

        # Helper procedures (ad-hoc classes like _MoveToProcedure,
        # _PickUpAndSmelt, etc.) don't subclass Procedure and therefore
        # don't auto-write action_logs. Supervisor reads action_logs to
        # detect idle planner loops, so writing an entry here keeps long
        # helper-driven tours (e.g. COLLECTING pile visits) visible as
        # liveness.
        if result is not None and not isinstance(proc, Procedure) and aborted is None:
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

        # Track move outcomes in location stats so the scoring
        # system learns which destinations are reachable.
        if result and isinstance(proc, _MoveToProcedure):
            loc_name = proc.name.removeprefix("move_to_")
            if result.success:
                self._roaming._location_stats.record_visit(loc_name, success=True)
            else:
                self._roaming._location_stats.record_visit(loc_name, success=False)
                self._roaming._location_stats.record_failure(loc_name)
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

    def _should_flee_swarm(self, ctx: AgentContext, ss) -> bool:
        """Priority 1b decision: flee a swarm now, or fall through to heal?

        Returns True while the agent is wounded (HP < _FLEE_HP_PCT) or poisoned, AND swarmed
        (>= _FLEE_HOSTILE_COUNT live hostiles within 6 tiles) — UNLESS it has
        already chosen to flee _FLEE_MAX_CONSECUTIVE ticks in a row without
        breaking contact. A fully-surrounded agent cannot path away, but
        _FleeFromHostiles.run() still reports success on the no-op move, so the
        unconditional gate used to re-select flee every tick forever — the agent
        "fled in place" while the swarm beat it to death, never reaching the
        heal-in-place block right below the gate. After the cap we stop fleeing
        and let it bandage/heal: an interrupted bandage is a chance at survival,
        an infinite flee loop is not. The counter resets the moment the
        wounded+swarmed condition no longer holds (contact broken or healed up).

        Poison is folded into the trigger alongside the HP floor. The Priority-1c
        cure gate applies ``bandage_self`` in place the moment the agent is
        poisoned (even at full HP), but a bandage — heal OR cure — is cancelled
        by adjacent melee, the exact reason this flee branch exists. A poisoned
        agent at >= _FLEE_HP_PCT HP surrounded by a swarm therefore slips past
        the HP-only flee floor AND the heal-in-place floor, reaches the cure
        gate, and re-attempts a cure-bandage every tick that the swarm keeps
        interrupting — bleeding/poisoning to death without ever breaking
        contact. Treat a poisoned-and-swarmed agent exactly like a wounded one:
        flee first so the un-swarmed cure gate can actually land the bandage.
        """
        if ss.hits_max <= 0:
            self._flee_consecutive = 0
            return False
        too_wounded = ss.hits < ss.hits_max * _FLEE_HP_PCT
        poisoned = bool(getattr(ss, "is_poisoned", False))
        swarmed = (
            (too_wounded or poisoned)
            and _count_hostiles(ctx, ss, dist=_FLEE_SCAN_DIST) >= _FLEE_HOSTILE_COUNT
        )
        if not swarmed:
            self._flee_consecutive = 0
            return False
        self._flee_consecutive += 1
        if self._flee_consecutive > _FLEE_MAX_CONSECUTIVE:
            logger.warning(
                "planner_flee_gave_up",
                consecutive=self._flee_consecutive,
                hp=f"{ss.hits}/{ss.hits_max}",
            )
            return False
        return True

    def _should_heal_in_place(self, ss) -> bool:
        """Priority-1 decision: heal/bandage in place because we're too
        wounded to keep fighting.

        The floor is _HEAL_IN_PLACE_HP_PCT, deliberately raised to combat's
        re-engage floor (hunt_nearby.can_start needs >= 40% HP) rather than the
        old hardcoded 0.30. Combat breaks off at RETREAT_HP_PCT (35%) and will
        not re-hunt until 40%, while the Priority-1b flee gate above only fires
        when SWARMED (>= _FLEE_HOSTILE_COUNT hostiles). That left a real dead
        zone: at 30–40% HP with only one or two hostiles (or a kiting mob just
        out of melee), the agent could neither re-hunt (too wounded), nor flee
        (not swarmed), nor heal (gate was < 0.30) — so it fell through to the
        profession / mining loop and wandered wounded until it re-aggroed and
        died. Healing across the whole 30–40% band closes that zone for every
        persona, not just the adventurer (whose PROFESSION_LOOPS happens to lead
        with bandage_self). Mirrors the flee gate's own 0.40 floor so the two
        survival branches agree on what "too wounded to fight" means.
        """
        return ss.hits_max > 0 and ss.hits < ss.hits_max * _HEAL_IN_PLACE_HP_PCT

    def _should_cure_poison(self, ss) -> bool:
        """Priority-1 decision: a poison tick is a survival threat that the
        HP-percentage gates above CANNOT see.

        ``_should_flee_swarm`` and ``_should_heal_in_place`` both key purely on
        HP fraction. Poison is invisible to them: a freshly-poisoned agent sits
        at high HP (>= 40%) so neither fires, and it falls straight through to
        the Priority-1.5 profession / mining loop — where bandages never fire
        (combat_loop's poison override only runs *inside* a fight, and combat
        has ended once the mob is dead/fled). The poison then ticks the agent
        down, and by the time HP finally crosses the 40% heal floor the agent is
        mid-procedure (mining/smelting/crafting) and keeps bleeding to death.

        ServUO's Bandage.cs cures poison on a roll *independent* of the heal, so
        a cure-bandage is worth applying even at full HP. This gate mirrors
        combat_loop._maybe_bandage's documented poison override at the planner
        level, so the agent stops everything and cure-bandages the moment it is
        poisoned, instead of only reacting once the poison has nearly killed it.
        """
        return bool(getattr(ss, "is_poisoned", False))

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
            if self._proc_breaker.is_open(name):
                # Starvation guard: temporarily demoted after consecutive
                # failures — fall through to lower-priority work.
                logger.debug("planner_starvation_skip", procedure=name)
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

        # --- Priority 0: Death recovery (ghost state) ---
        # If the agent is dead (HP=0), nothing else can work — every action
        # returns "I am dead and cannot do that". Walk to a healer NPC.
        if not ss.is_alive:
            # Arm post-resurrection recovery here, in the branch that actually
            # fires on death. Priority 1's dead-branch (which used to set this)
            # is unreachable while dead — Priority 0 returns first — so without
            # this the agent revives naked next to an un-looted corpse and
            # resumes the profession loop unarmed → immediate re-death.
            ctx.blackboard["_was_dead"] = True
            seek_fails = self._repeat_counter.get("seek_resurrection", 0)
            DEATH_ESCALATE_THRESHOLD = 5
            if seek_fails >= DEATH_ESCALATE_THRESHOLD:
                # seek_resurrection is deadlocked: every healer location
                # is either unreachable or unresponsive. Clear the counter
                # and rotation state, then post a forum help request.
                ctx.blackboard["planner_intent"] = (
                    f"사망 복구 교착 ({seek_fails}회 실패) → "
                    f"포럼 도움 요청 + 힐러 목록 초기화"
                )
                logger.warning(
                    "planner_dead_escalating",
                    seek_fails=seek_fails,
                    pos=f"({ss.x},{ss.y})",
                )
                self._repeat_counter["seek_resurrection"] = 0
                return _DeathEscalate(self._deadlock)

            ctx.blackboard["planner_intent"] = (
                f"사망 상태 (HP {ss.hits}/{ss.hits_max}) → 힐러 NPC 찾아 부활 시도"
            )
            logger.info("planner_dead_seeking_resurrection", hp=f"{ss.hits}/{ss.hits_max}")
            return _SeekResurrection()

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
                if (proc and not self._proc_breaker.is_open("heal_self")
                        and await proc.can_start(ctx)):
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
        if ss.hits_max > 0 and ss.hits <= 0:
            # DEAD — surface prominently so supervisor/user sees it.
            logger.warning("agent_dead", hp=ss.hits, hp_max=ss.hits_max)
            if ctx.bus is not None:
                try:
                    ctx.bus.publish("planner.death", {
                        "message": f"☠ 사망 (HP {ss.hits}/{ss.hits_max})",
                        "importance": 5,
                    })
                except Exception:
                    pass
            _intent(f"사망 상태 — 부활 필요")
            ctx.blackboard["_was_dead"] = True  # remember for post-res recovery
            proc = self.registry.get("seek_resurrection")
            if proc and await proc.can_start(ctx):
                return proc
            return None

        # --- Priority 1a: Post-resurrection recovery ---
        # Just revived (was dead, now alive): the agent stands naked next to
        # its corpse. Recover the gear and re-equip before resuming, instead of
        # running the profession loop unarmed (→ immediate re-death).
        if ctx.blackboard.get("_was_dead") and ss.is_alive:
            # The dead→alive transition is the one reliable place to clear the
            # death-recovery escalation counter. It is bumped on every failed
            # seek_resurrection (Priority 0) and only zeroed on that procedure's
            # own success tick — but a resurrection that happens out of band (a
            # wandering healer, another player, or a gump answered during the
            # _DeathEscalate forum wait) brings us back alive WITHOUT that tick,
            # leaving a stale count behind. Next death then escalates to the
            # forum after far fewer than DEATH_ESCALATE_THRESHOLD failures —
            # dragging the agent into a 5-minute help-wait it doesn't need.
            self._repeat_counter["seek_resurrection"] = 0
            recover = _RecoverAfterDeath()
            if await recover.can_start(ctx):
                _intent("부활 직후 — 시체 회수 + 재장착")
                return recover
            ctx.blackboard["_was_dead"] = False  # nothing left to recover

        # --- Priority 1b: Flee a swarm before healing ---
        # Bandaging in place fails when surrounded (melee cancels it), so a
        # wounded warrior facing several hostiles breaks contact FIRST.
        # This gate is its own HP floor (_FLEE_HP_PCT), deliberately HIGHER
        # than the in-place heal floor (0.30) below: a hunt breaks off at
        # RETREAT_HP_PCT (35%), and once it does the planner re-selects — at
        # 30–35% HP, still surrounded, the old code skipped this flee (nested
        # inside the <30% block) and bandaged in place, where melee cancels the
        # bandage and the agent dies. Fleeing across the full 30–40% band
        # closes that death zone.
        if self._should_flee_swarm(ctx, ss):
            _intent("HP 위험 + 다수 적 → 후퇴 후 치료")
            return _FleeFromHostiles()

        if self._should_heal_in_place(ss):
            logger.warning("agent_low_hp", hp=ss.hits, hp_max=ss.hits_max)
            if ctx.bus is not None:
                try:
                    ctx.bus.publish("planner.critical_hp", {
                        "message": f"⚠ HP 위험 ({ss.hits}/{ss.hits_max})",
                        "importance": 4,
                    })
                except Exception:
                    pass
            proc = self.registry.get("heal_self")
            if (proc and not self._proc_breaker.is_open("heal_self")
                    and await proc.can_start(ctx)):
                _intent(f"HP 위험 ({ss.hits}/{ss.hits_max}) → 치료")
                return proc
            # Bandages heal too (and grind Healing) — combat personas
            # carry them from creation. Fetch it DIRECTLY (not via _get_proc)
            # so the anti-thrash starvation breaker cannot silence the
            # last-resort survival heal: a bandage interrupted by an adjacent
            # mob fails, so under sustained pressure the breaker demotes
            # bandage_self — and with heal_self also unavailable (no potions),
            # both survival heals would return None and the critically-wounded
            # agent falls straight through to the profession / mining loop and
            # bleeds to death. Survival is non-negotiable; mirror heal_self's
            # own direct fetch above.
            proc = self.registry.get("bandage_self")
            if proc and await proc.can_start(ctx):
                _intent(f"HP 위험 ({ss.hits}/{ss.hits_max}) → 붕대 치료")
                return proc

        # --- Priority 1c: Cure an active poison (independent of HP) ---
        # Poison is invisible to the HP-percentage gates above: it ticks an
        # otherwise-healthy agent down while it runs the profession/mining loop
        # (where combat_loop's in-fight poison-bandage override no longer runs).
        # Cure-bandage NOW so the agent doesn't keep grinding while it bleeds to
        # death. bandage_self.can_start admits a poisoned agent even at full HP.
        #
        # Fetch bandage_self DIRECTLY (not via _get_proc), mirroring the
        # critically-wounded heal-in-place block above: a cure is a survival
        # action, and the anti-thrash starvation breaker must never silence it.
        # Poison most often lands in combat, where an adjacent mob keeps
        # interrupting the bandage — exactly the failure pattern that demotes
        # bandage_self in self._proc_breaker. Routing the poison cure through
        # _get_proc let that demotion return None, dropping the agent straight
        # into the profession/mining loop to bleed to death from the very
        # poison this gate exists to clear. Survival is non-negotiable.
        if self._should_cure_poison(ss):
            proc = self.registry.get("bandage_self")
            if proc and await proc.can_start(ctx):
                _intent(
                    f"중독 (lvl {getattr(ss, 'poison_level', 0)}) → 해독 붕대"
                )
                return proc

        # --- Priority 1.5: Profession loop (non-mining trades) ---
        # Personas with an explicit profession run their trade procedure
        # as the primary activity; the mining chain below stays as the
        # default for miner-type personas and as the universal fallback.
        profession = getattr(ctx.persona, "profession", "") or ""
        # Loot-flush gate: an adventurer accumulates vendor-sellable gear
        # from corpses but bandage_self/hunt_nearby always win the
        # first-startable loop, so the looted gear never converts to gold.
        # When a backlog has piled up, prefer sell_to_vendor — its can_start
        # already verifies a vendor is nearby, so when none is in range this
        # falls through to normal hunting (no thrash).
        #
        # ...BUT only between fights. The flush preempts the WHOLE profession
        # loop (hunt_nearby included), so without a combat guard a backlog of 6+
        # looted items pulls the warrior OUT of a live, winnable fight every loop
        # to walk to the vendor — in a survival arena (vendor co-located with the
        # spawn) that means repeatedly turning its back on engaged mobs and
        # bleeding out. Yield to an in-range combat target, exactly like the
        # standalone-bandage gate above; selling resumes once the fight is over.
        if (
            profession == "adventurer"
            and _count_sellable_loot_in_pack(ctx) >= LOOT_SELL_BACKLOG_THRESHOLD
            and not _loot_flush_should_yield_to_combat(ctx)
        ):
            sell = _get_proc("sell_to_vendor")
            if sell and await sell.can_start(ctx):
                _intent(f"직업 활동 ({profession}) → 전리품 판매 (sell_to_vendor)")
                return sell
        for proc_name in PROFESSION_LOOPS.get(profession, ()):
            # bandage_self leads the adventurer loop as a SURVIVAL fallback, but
            # its can_start admits any wound below 95% HP (and first-startable
            # wins). With a hostile in range that pulls a barely-scratched
            # warrior OUT of combat to apply a low-value bandage every loop —
            # forfeiting swings, the COMBAT-uptime leak behind the engaged-vs-
            # idle seed variance. The real survival heals already ran above
            # (Priority-1 heal-in-place < 40%, Priority-1c poison cure), and an
            # active fight heals itself: hunt_nearby interleaves _maybe_bandage
            # at 85%. So when a target is in range and we're above the retreat
            # floor, skip standalone bandage_self and let hunt_nearby lead.
            if proc_name == "bandage_self" and _bandage_should_yield_to_combat(ctx, ss):
                continue
            proc = _get_proc(proc_name)
            if proc and await proc.can_start(ctx):
                _intent(f"직업 활동 ({profession}) → {proc_name}")
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
            # Post-forum relocate has absolute priority inside P4: if the
            # agent already asked for help in this city and nothing came,
            # don't let the buy/craft branches drag it back — leave town.
            # (Mirror clear-when-far logic so a relocating agent resumes
            # normal recovery once it reaches a fresh area.)
            _pf_stranded = ctx.blackboard.get("_forum_stranded_pos")
            if ctx.blackboard.get("_post_forum_relocate") and _pf_stranded:
                dist_from_stranded = max(
                    abs(ss.x - _pf_stranded[0]),
                    abs(ss.y - _pf_stranded[1]),
                )
                if dist_from_stranded > 60:
                    ctx.blackboard.pop("_post_forum_relocate", None)
                    ctx.blackboard.pop("_forum_stranded_pos", None)
                    logger.info(
                        "planner_post_forum_relocate_cleared",
                        moved=dist_from_stranded,
                    )
                else:
                    # Abandon the post-forum relocate when relocate_to_city
                    # itself keeps failing: Britain is unreachable from the
                    # current position (long-distance A* exhausting its
                    # step budget, terrain trap, etc.), so looping on the
                    # same waypoint just burns ticks.  Fall back to
                    # _YellForHelp — the only recovery that does not depend
                    # on pathfinding — and let the deadlock ladder resume
                    # from Lv6 on the next tick.
                    _relocate_fails = self._repeat_counter.get(
                        "relocate_to_city", 0,
                    )
                    if _relocate_fails >= 5:
                        ctx.blackboard.pop("_post_forum_relocate", None)
                        ctx.blackboard.pop("_forum_stranded_pos", None)
                        self._repeat_counter["relocate_to_city"] = 0
                        ctx.blackboard["_deadlock_recovery_level"] = 6
                        ctx.blackboard["_deadlock_attempt_count"] = 0
                        logger.warning(
                            "planner_post_forum_relocate_abandoned",
                            fails=_relocate_fails,
                            pos=f"({ss.x},{ss.y})",
                        )
                        _intent(
                            "교착 복구: 다른 도시 이동 경로 차단됨 → "
                            "제자리 외치기로 전환"
                        )
                        return _YellForHelp(ss)
                    _intent(
                        "교착 복구: 포럼 요청 후 도움 없음 → 다른 도시로 이동"
                    )
                    return _RelocateToCity(ss)

            # 4a: Has SMELTABLE ore → smelt first.
            # `ore_count` includes ore stacks the smelt step has already given
            # up on (unsmelable hue, or 1-amount iron with no merge partner).
            # Gating on raw ore_count traps the planner in a forge-bound loop
            # because smelt_ore.can_start filters those same serials → never
            # advances to make_tools/sell paths.
            if smeltable_ore > 0:
                proc = _get_proc("smelt_ore")
                if proc and await proc.can_start(ctx):
                    _intent("곡괭이 없음, 광석 보유 → 제련부터")
                    return proc
                _intent("곡괭이 없음, 광석 보유, 용광로 없음 → 용광로로 이동")
                move = await self._roaming.move_to_location(ctx, "forge", "blacksmith")
                if move:
                    return move

            # 4b: Has tinker tools + ingots → try craft tools
            #     Skip if Tinkering gave up (skill too low) OR the
            #     skill-too-low block latch make_tools itself arms is still
            #     live. make_tools sets BOTH _make_tools_gave_up AND
            #     _tinkering_blocked_until on a "required skill" gump refusal,
            #     but deadlock recovery (deadlock.py strategy 4) and the
            #     mining-tool toggle (Priority 7) pop _make_tools_gave_up on
            #     their own — leaving _tinkering_blocked_until live yet
            #     un-honored here. Without this, a no-pickaxe smith below the
            #     Pickaxe minSkill re-selects make_tools every tick, opens the
            #     tinkering gump, hits "required skill" again, re-arms the 300s
            #     latch, and never reaches the 4c buy path — a tight gump loop.
            #     The 5e training gate already honors this latch; 4b must too.
            if (has_tinker_tools and ingot_count >= 4
                    and not ctx.blackboard.get("_make_tools_gave_up")
                    and time.time() >= ctx.blackboard.get("_tinkering_blocked_until", 0)):
                proc = _get_proc("make_tools")
                if proc and await proc.can_start(ctx):
                    _intent(f"곡괭이 없음, 주석도구+주괴 {ingot_count}개 → 도구 제작")
                    return proc

            # 4c: Buy tools from a tinker (pickaxe costs ~11 gold).
            # This server deducts vendor purchases from BANK GOLD, not
            # backpack — so ss.gold==0 is NOT a blocker. We cache the last
            # reading in `bank_balance`:
            #   - fresh + sufficient → proceed to buy_from_vendor
            #   - fresh + insufficient → disable buy, fall through
            #   - stale/missing + banker nearby → check_bank_balance first
            #   - stale/missing + no banker → blind try (old behavior);
            #     buy_from_vendor will self-disable on failure.
            buy_disabled_until = ctx.blackboard.get("_buy_disabled_until", 0)
            if time.time() >= buy_disabled_until:
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
                    ctx.blackboard["_buy_blind_walk_count"] = 0
                    _intent(
                        f"은행 잔액 {bal_amount}gp 부족 → 구매 건너뛰고 다른 경로 시도"
                    )
                    # Fall through to 4d/4e/4f below
                elif bal_fresh:
                    ctx.blackboard["_buy_blind_walk_count"] = 0
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
                        ctx.blackboard["_buy_blind_walk_count"] = 0
                        _intent("곡괭이 없음 → 은행 잔액 먼저 확인")
                        return cbb
                    # No banker reachable right now → blind buy attempt;
                    # buy_from_vendor self-disables on failure.
                    proc = _get_proc("buy_from_vendor")
                    if proc and await proc.can_start(ctx):
                        ctx.blackboard["_buy_blind_walk_count"] = 0
                        _intent(
                            f"곡괭이 없음, 금화 {ss.gold}g (은행 미확인) → 상점에서 구매"
                        )
                        return proc
                    # Cap the blind walk-to-bank/vendor loop: with an empty
                    # bank, check_bank_balance never fires (banker out of
                    # 12-tile range until we arrive) and buy_from_vendor
                    # never fires (no vendor in range either), so the agent
                    # ping-pongs between vendor locations forever without
                    # ever reaching Priority 4f deadlock recovery.  After a
                    # few cycles, skip the move and fall through so hunt/
                    # scavenge/relocate paths get a chance to run.
                    _blind_walks = ctx.blackboard.get(
                        "_buy_blind_walk_count", 0,
                    )
                    if _blind_walks < 3:
                        ctx.blackboard["_buy_blind_walk_count"] = (
                            _blind_walks + 1
                        )
                        _intent(
                            f"곡괭이 없음, 금화 {ss.gold}g → 은행/상점으로 이동"
                        )
                        move = await self._roaming.move_to_location(
                            ctx, "bank", "tinker", "provisioner", "miner",
                        )
                        if move:
                            return move
                    else:
                        logger.info(
                            "planner_buy_blind_walks_exhausted",
                            count=_blind_walks,
                            pos=f"({ss.x},{ss.y})",
                        )
                        # Leave the counter set — it resets only when the
                        # balance cache goes fresh (see Priority 4c head).

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

            # When buy is disabled (bank empty) AND we have no ingots/ore,
            # the standard crafting and resale paths are all closed.  Fall
            # through to Priority 4f deadlock recovery instead of stopping
            # — the recovery ladder (hunt rats with a dagger, scavenge
            # ground items, relocate to a different city, yell for help,
            # forum escalation) has several concrete options that do not
            # require user intervention.  The supervisor still has the
            # deadlock alert for cases where operator action truly is
            # needed; the planner's job here is to keep trying.
            buy_disabled = time.time() < ctx.blackboard.get("_buy_disabled_until", 0)
            if buy_disabled and ingot_count == 0 and ore_count == 0:
                logger.info(
                    "planner_fallthrough_to_deadlock_recovery",
                    reason="no tool, no ingots, buy disabled",
                )

            # 4f: TRUE DEADLOCK — no tools, no gold, no ore, no ingots
            # Progressive recovery:
            #   Level 0: sell junk to vendor / scavenge nearby (3 attempts)
            #   Level 1: drop ALL unsellable junk to unblock strategies
            #   Level 2: wander-explore (random walk scanning for items)
            #   Level 3: beg at bank — walk to populated area, ask for help
            #   Level 4: relocate to different city (Britain)
            #   Level 5: hunt monsters for gold
            #   Level 6+: forum escalation, then reset cycle
            logger.info("planner_deadlock_recovery_attempt")

            # Don't clear _failed_destinations or _move_fail_until here.
            # Their natural cooldowns (5 min / 30s) cause move_to_location
            # to return None for exhausted destinations, letting the code
            # fall through to the escalation check instead of retrying the
            # same blocked paths forever.

            _deadlock_attempts = ctx.blackboard.get("_deadlock_attempt_count", 0)
            _deadlock_level = ctx.blackboard.get("_deadlock_recovery_level", 0)

            # Cross-cycle fast-forward.  The Lv0-Lv7 ladder resets to Lv0
            # after the forum escalation at level 7, so a truly dead city
            # re-walks Lv0-Lv3 (sell/drop/wander/beg) each cycle before
            # reaching Lv4 (_RelocateToCity) — the only strategy that
            # actually moves the agent to different terrain.  When the full
            # ladder has already completed at least once in the current
            # area, jump straight to Lv4 (or Lv6 _YellForHelp if relocate
            # has been blocked) instead of repeating the slow climb.
            _cycles = ctx.blackboard.get("_deadlock_cycles_completed", 0)
            _last_cycle_pos = ctx.blackboard.get("_deadlock_last_cycle_pos")
            if (
                _cycles >= 1
                and _deadlock_level == 0
                and _last_cycle_pos is not None
            ):
                _cycle_drift = max(
                    abs(ss.x - _last_cycle_pos[0]),
                    abs(ss.y - _last_cycle_pos[1]),
                )
                # 40 tiles ≈ one city block; further than that and the
                # low-level strategies may still find fresh vendors/loot.
                if _cycle_drift <= 40:
                    _relocate_fails = self._repeat_counter.get(
                        "relocate_to_city", 0,
                    )
                    if _relocate_fails >= 5:
                        # Relocate has been failing — the city isn't just
                        # dead, it's pathfinding-trapped.  Fall back to
                        # stationary yelling (no A*).
                        self._repeat_counter["relocate_to_city"] = 0
                        ctx.blackboard["_deadlock_recovery_level"] = 6
                        ctx.blackboard["_deadlock_attempt_count"] = 0
                        logger.warning(
                            "planner_deadlock_cycle_repeat_yell",
                            cycles=_cycles,
                            drift=_cycle_drift,
                            pos=f"({ss.x},{ss.y})",
                        )
                        _intent(
                            f"교착 복구: Lv0-Lv7 {_cycles}회 완주 + "
                            "이동 차단 → 제자리 외치기"
                        )
                        return _YellForHelp(ss)
                    ctx.blackboard["_deadlock_recovery_level"] = 4
                    ctx.blackboard["_deadlock_attempt_count"] = 0
                    logger.warning(
                        "planner_deadlock_cycle_repeat_relocate",
                        cycles=_cycles,
                        drift=_cycle_drift,
                        last_pos=_last_cycle_pos,
                    )
                    _intent(
                        f"교착 복구: Lv0-Lv7 {_cycles}회 완주 → "
                        "Lv4 단축 (다른 도시로 이동)"
                    )
                    return _RelocateToCity(ss)

            # Post-forum memory: escalate_to_forum sets _post_forum_relocate
            # and records the stranded position when the 5-min help wait
            # produced no gold or tool.  Without this, the full-reset inside
            # escalate_to_forum drops us back to Level 0 here and the same
            # Level 0→6 cycle replays in the same city indefinitely.  Jump
            # straight to Level 4 (relocate) to escape the dead-end area.
            _stranded_pos = ctx.blackboard.get("_forum_stranded_pos")
            if ctx.blackboard.get("_post_forum_relocate") and _stranded_pos:
                dist_from_stranded = max(
                    abs(ss.x - _stranded_pos[0]),
                    abs(ss.y - _stranded_pos[1]),
                )
                # Clear once we've moved far enough that a normal recovery
                # has a reasonable chance — the Britain waypoint is ~1000
                # tiles from Minoc so 60 tiles is well into new terrain.
                if dist_from_stranded > 60:
                    ctx.blackboard.pop("_post_forum_relocate", None)
                    ctx.blackboard.pop("_forum_stranded_pos", None)
                    logger.info(
                        "planner_post_forum_relocate_cleared",
                        moved=dist_from_stranded,
                    )
                else:
                    # Abandon the post-forum relocate when relocate_to_city
                    # itself keeps failing: Britain is unreachable from the
                    # current position (long-distance A* exhausting its
                    # step budget, terrain trap, etc.), so looping on the
                    # same waypoint just burns ticks.  Fall back to
                    # _YellForHelp — the only recovery that does not depend
                    # on pathfinding — and let the deadlock ladder resume
                    # from Lv6 on the next tick.
                    _relocate_fails = self._repeat_counter.get(
                        "relocate_to_city", 0,
                    )
                    if _relocate_fails >= 5:
                        ctx.blackboard.pop("_post_forum_relocate", None)
                        ctx.blackboard.pop("_forum_stranded_pos", None)
                        self._repeat_counter["relocate_to_city"] = 0
                        ctx.blackboard["_deadlock_recovery_level"] = 6
                        ctx.blackboard["_deadlock_attempt_count"] = 0
                        logger.warning(
                            "planner_post_forum_relocate_abandoned",
                            fails=_relocate_fails,
                            pos=f"({ss.x},{ss.y})",
                        )
                        _intent(
                            "교착 복구: 다른 도시 이동 경로 차단됨 → "
                            "제자리 외치기로 전환"
                        )
                        return _YellForHelp(ss)
                    _intent(
                        "교착 복구: 포럼 요청 후 도움 없음 → 다른 도시로 이동"
                    )
                    return _RelocateToCity(ss)

            # --- Stuck-in-place shortcut to Lv6 (_YellForHelp) ---
            # When every named-destination A* dies (observed pattern:
            # 3×`move_to_<vendor>→blocked` + 3×`hunt_for_gold→blocked`
            # with small drift in position), the agent is in a local
            # pathfinding trap: every Lv2-Lv5 strategy depends on A* and
            # will keep failing the same way.  Only Lv6 (_YellForHelp) is
            # stationary-no-pathfinding.  Detect the trap via drift from
            # the first entry of the current recovery cycle and jump
            # straight to Lv6 once we've had a few chances at lower
            # levels.  Gated on Lv≥2 because Lv0-1 (sell/drop junk) does
            # not depend on pathfinding, so the counter escalation there
            # is already the right path.
            _first_pos = ctx.blackboard.get("_deadlock_first_pos")
            if _first_pos is None:
                ctx.blackboard["_deadlock_first_pos"] = (ss.x, ss.y)
            elif 2 <= _deadlock_level < 6 and _deadlock_attempts >= 3:
                drift = max(
                    abs(ss.x - _first_pos[0]),
                    abs(ss.y - _first_pos[1]),
                )
                if drift <= 8:
                    logger.warning(
                        "planner_deadlock_stuck_shortcircuit",
                        level=_deadlock_level,
                        attempts=_deadlock_attempts,
                        drift=drift,
                        pos=f"({ss.x},{ss.y})",
                    )
                    ctx.blackboard["_deadlock_recovery_level"] = 6
                    ctx.blackboard["_deadlock_attempt_count"] = 0
                    _intent(
                        "교착 복구: 경로 전면 차단 감지 → "
                        "Lv6 외치기로 단축 에스컬레이션"
                    )
                    return _YellForHelp(ss)

            # Gold-progress anti-escalation: if the agent is earning gold
            # (e.g. from killing rats), reset the attempt counter so it
            # stays at the current level and keeps hunting instead of
            # escalating past a working strategy.
            _prev_gold = ctx.blackboard.get("_deadlock_last_gold", 0)
            if ss.gold > _prev_gold:
                logger.info(
                    "deadlock_gold_progress",
                    prev=_prev_gold, now=ss.gold,
                )
                _deadlock_attempts = 0
                ctx.blackboard["_deadlock_attempt_count"] = 0
                # Real progress — clear the cross-cycle fast-forward so the
                # agent returns to the normal Lv0 ladder on the next deadlock.
                ctx.blackboard.pop("_deadlock_cycles_completed", None)
                ctx.blackboard.pop("_deadlock_last_cycle_pos", None)
                # Begging evidently worked — let it try again next time.
                ctx.blackboard.pop("_beg_consecutive_fails", None)
                # Same reasoning for hunting: fresh gold means the hunt
                # loop is no longer monopolising Lv0, so re-arm the
                # opportunistic close-range hunt hook.
                ctx.blackboard.pop("_hunt_consecutive_fails", None)
            ctx.blackboard["_deadlock_last_gold"] = ss.gold

            # Always count visits — this drives escalation even when
            # nothing is found (unlike the old scavenge-only counter).
            ctx.blackboard["_deadlock_attempt_count"] = _deadlock_attempts + 1

            # Compute item-aware vendor keywords from actual backpack contents
            # so we walk to the RIGHT vendor type (e.g. "arms" for daggers).
            # Rotate through vendor types one at a time so the agent tries
            # blacksmith, then tailor, then provisioner — not all at once
            # (which picks the nearest, often the wrong type).
            _all_vendor_kw = self._sellable_vendor_keywords(ctx, ss)
            _vendor_idx = ctx.blackboard.get("_deadlock_vendor_idx", 0)
            _sell_vendor_kw = [_all_vendor_kw[_vendor_idx % len(_all_vendor_kw)]] if _all_vendor_kw else []

            # --- Buy tools escape hatch ---
            # If the agent accumulated gold (from hunting rats, begging,
            # or selling junk), try buying a pickaxe immediately.  The
            # normal buy-tools logic lives at Priority 4.5 which is
            # AFTER the deadlock block — the agent can never reach it
            # while trapped here.  This closes the loop:
            # hunt rats → gold → buy pickaxe → exit deadlock.
            if ss.gold >= 10:
                proc = _get_proc("buy_from_vendor")
                if proc and await proc.can_start(ctx):
                    _intent("교착 복구: 금화 확보됨 → 도구 구매 시도")
                    return proc
                # No vendor nearby — walk to a blacksmith to buy tools
                if time.time() > self._move_fail_until:
                    move = await self._roaming.move_to_location(
                        ctx, "blacksmith", "arms", "provisioner",
                    )
                    if move:
                        _intent("교착 복구: 금화 보유 → 도구 상점으로 이동")
                        return move

            # --- Opportunistic NPC begging (Lv0 only) ---
            # Zero-precondition gold source: UO's Begging skill (ID 6)
            # yields 1-5gp per successful attempt on an NPC.  With a
            # pickaxe costing ~15gp, a few successful begs bootstrap out
            # of the deadlock without combat capability, tools, or other
            # players being online — covering the gap between Lv0
            # sell-junk (requires sellable inventory) and the Lv2-5
            # ladder (requires combat or relocation progress).  Gated on
            # Lv0 so it doesn't monopolize the ladder; the per-serial
            # cooldown in _find_beg_npc prevents re-begging the same NPC.
            # The consecutive-fails cap stops the re-target loop once the
            # agent's Karma/Begging level means no local NPC will ever
            # give coin — without this, two visible refusers keep firing
            # on their 30s cooldowns and Lv0 never escalates to Lv1.
            _beg_fails = ctx.blackboard.get("_beg_consecutive_fails", 0)
            if (
                _deadlock_level == 0
                and ss.gold < 10
                and _beg_fails < _BegNpcForCoin.MAX_CONSECUTIVE_FAILS
            ):
                _beg_target = self._find_beg_npc(ctx, ss)
                if _beg_target is not None:
                    _intent(
                        f"교착 복구 Lv0: {_beg_target.name or 'NPC'}에게 구걸 시도"
                    )
                    return _BegNpcForCoin(ss, _beg_target)

            # --- Opportunistic wood-chopping (any level) ---
            # Alternate resource path when ore is blocked: a hatchet in
            # backpack + any tree in view yields logs the agent can later
            # sell to a provisioner or smith for a pickaxe.  Option 5 from
            # the deadlock playbook — "try a different resource".  Hatchets
            # are picked up by scavenging (see deadlock.find_ground_valuables),
            # so this path activates as soon as one is acquired.
            if self._has_hatchet(ctx, ss):
                proc = _get_proc("chop_wood")
                if proc and await proc.can_start(ctx):
                    _intent("교착 복구: 손도끼 + 나무 발견 → 벌목으로 자원 확보")
                    return proc
                # Have a hatchet but no tree in sight — walk toward a
                # forested area before retrying.
                if time.time() > self._move_fail_until:
                    move = await self._roaming.move_to_location(
                        ctx, "forest", "woods", "lumber", "camp",
                    )
                    if move:
                        _intent("교착 복구: 손도끼 보유 → 숲으로 이동")
                        return move

            # --- Opportunistic close-range hunt (any level) ---
            # Easy prey (rat/cat) standing within a few tiles is the
            # shortest path to gold — a dagger-armed agent typically earns
            # 1-5gp per kill in classic UO, enough to bootstrap a pickaxe
            # purchase after one or two kills.  Firing this before the
            # sell/walk logic short-circuits the minutes-long Lv4 relocate
            # walk whenever the deadlock has prey in immediate view.
            #
            # The consecutive-fail cap is the escape valve: when the
            # agent can't actually kill what it attacks (observed: 8×
            # `Could not kill a rat/cat/woodpecker` with a starter
            # dagger), this hook would otherwise re-fire every Lv0 tick
            # and return a _HuntForGold procedure before the
            # escalation check at line ~1265, pinning the ladder at Lv0
            # forever.  Cap mirrors _BegNpcForCoin and is cleared on
            # gold progress / Lv7 cycle reset / deadlock exit.
            _hunt_fails = ctx.blackboard.get("_hunt_consecutive_fails", 0)
            if (
                self._has_usable_weapon(ctx, ss)
                and _hunt_fails < _HuntForGold.MAX_CONSECUTIVE_FAILS
            ):
                _close_prey = self._find_huntable_target(
                    ctx, ss, include_small_animals=True,
                )
                if _close_prey is not None:
                    _prey_dist = max(
                        abs(_close_prey.x - ss.x),
                        abs(_close_prey.y - ss.y),
                    )
                    if _prey_dist <= 5:
                        _intent(
                            f"교착 복구: 근거리 {_close_prey.name or '사냥감'}"
                            f" 발견 → 즉시 사냥"
                        )
                        return _HuntForGold(_close_prey, ss)
                    # Lv0 medium-range hunt: after the first vendor-sell
                    # attempt fails, prefer hunting any visible reachable
                    # prey over continuing the sell→walk→refusal cycle.
                    # _find_huntable_target has already A*-verified the
                    # target, and a rat kill (1-5gp) bootstraps a pickaxe
                    # purchase — a shorter path out of the deadlock than
                    # rotating through 5+ escalation levels.
                    if _deadlock_level == 0 and _deadlock_attempts >= 1:
                        _intent(
                            f"교착 복구 Lv0: 원거리 "
                            f"{_close_prey.name or '사냥감'}"
                            f" ({_prey_dist}타일) → 판매 대신 사냥"
                        )
                        return _HuntForGold(_close_prey, ss)

            # --- Sell non-essential items for gold (Level 0 only) ---
            # Agent may have junk items (clothing, books, weapons) that
            # vendors will buy. Even a few gold can fund a pickaxe (~11g).
            # Gate on Level 0: once sell attempts fail 3 times and the
            # level escalates, stop retrying — the sell-junk block would
            # otherwise monopolize every tick and prevent escalation.
            if _deadlock_level == 0 and self._has_sellable_junk(ctx, ss):
                proc = _get_proc("sell_to_vendor")
                if proc and await proc.can_start(ctx):
                    _intent("교착 복구: 불필요 아이템 판매 → 금화 확보")
                    return proc
                # No vendor nearby → walk to matching vendor type
                # Rotate to next vendor type for subsequent attempts
                ctx.blackboard["_deadlock_vendor_idx"] = _vendor_idx + 1
                if time.time() > self._move_fail_until:
                    move = await self._roaming.move_to_location(
                        ctx, *_sell_vendor_kw,
                    )
                    if move:
                        _intent(f"교착 복구: {_sell_vendor_kw[0]} 상점으로 이동")
                        return move
                    # Named locations blocked → walk to a visible vendor NPC
                    vendor_npc = self._find_visible_vendor_npc(ctx, ss)
                    if vendor_npc:
                        _intent(f"교착 복구: 근처 상인 {vendor_npc.name or 'NPC'} 방문")
                        return _MoveToProcedure(
                            vendor_npc.name or "vendor",
                            vendor_npc.x, vendor_npc.y,
                        )

            # --- Sell if vendor already within range (any level) ---
            # At levels 1+ we don't walk to vendors (that caused the
            # monopolisation bug fixed in 11a8cba).  But if exploration
            # already positioned us near a vendor, seize the chance.
            if _deadlock_level >= 1 and self._has_sellable_junk(ctx, ss):
                proc = _get_proc("sell_to_vendor")
                if proc and await proc.can_start(ctx):
                    _intent("교착 복구: 인근 상인 발견 → 아이템 판매 시도")
                    return proc

            # After 3 attempts at current level, escalate to next.
            if _deadlock_attempts >= 3:
                _deadlock_level += 1
                ctx.blackboard["_deadlock_recovery_level"] = _deadlock_level
                ctx.blackboard["_deadlock_attempt_count"] = 0
                # Fresh state for the new level so higher-level strategies
                # aren't blocked by stale entries from previous levels.
                self._failed_destinations.clear()
                # NOTE: refused_vendors is NOT cleared here. Previously we
                # cleared on Lv0→Lv1 assuming drop_junk_items changed
                # inventory, so a refused vendor might now accept what
                # remained. But blessed/newbified items silently fail to
                # drop (server 0x27), so the inventory often did NOT
                # change and the cleared refused vendor would reject us
                # again → tight loop. _DropJunkItems now clears
                # refused_vendors itself, but only when at least one drop
                # actually took effect.
                # Reset vendor rotation index for the new level
                ctx.blackboard.pop("_deadlock_vendor_idx", None)
                logger.warning(
                    "planner_deadlock_escalating",
                    attempts=_deadlock_attempts,
                    new_level=_deadlock_level,
                    weight=f"{ss.weight}/{ss.weight_max}",
                )

                # Level 1: Drop ALL unsellable junk
                # Purpose isn't reducing weight — it's clearing the
                # _has_sellable_junk flag so future Level-0 retries
                # (after a full reset) won't re-trap the agent.
                if _deadlock_level == 1:
                    if self._has_sellable_junk(ctx, ss):
                        _intent("교착 복구 Lv1: 판매 불가 아이템 버리기")
                        return _DropJunkItems(ss)

                # Level 2: Wander-explore — random walk around town
                elif _deadlock_level == 2:
                    _intent("교착 복구 Lv2: 마을 주변 탐색 (랜덤 이동)")
                    return _WanderAndScavenge(ss)

                # Level 3: Beg at bank — walk to populated area, ask for help
                elif _deadlock_level == 3:
                    _intent("교착 복구 Lv3: 은행/광장으로 이동하여 도움 요청")
                    return _BegAtBank(ss)

                # Level 4: Relocate to a different city (Britain)
                # Local town is exhausted — walk to Britain which has
                # more vendor variety, player traffic, and ground items.
                elif _deadlock_level == 4:
                    _intent("교착 복구 Lv4: 다른 도시로 이동 (Britain)")
                    return _RelocateToCity(ss)

                # Level 5: Hunt monsters for gold (including small animals)
                elif _deadlock_level == 5:
                    target = self._find_huntable_target(
                        ctx, ss, include_small_animals=True,
                    )
                    if target:
                        _intent(f"교착 복구 Lv5: {target.name or 'monster'} 사냥")
                        return _HuntForGold(target, ss)
                    # No targets in town — try wilderness, then town locations
                    if time.time() > self._move_fail_until:
                        move = await self._roaming.move_to_location(
                            ctx, "mine", "mining", "camp",
                        )
                        if move:
                            _intent("교착 복구 Lv5: 몬스터 없음 → 야외로 이동")
                            return move
                        # Wilderness blocked — try visible vendor NPC
                        vendor_npc = self._find_visible_vendor_npc(ctx, ss)
                        if vendor_npc:
                            _intent(f"교착 복구 Lv5: 근처 상인 {vendor_npc.name or 'NPC'} 방문")
                            return _MoveToProcedure(
                                vendor_npc.name or "vendor",
                                vendor_npc.x, vendor_npc.y,
                            )
                        # No visible vendors — try town locations
                        move = await self._roaming.move_to_location(
                            ctx, "bank", "tavern", "inn", "guild", "road",
                        )
                        if move:
                            _intent("교착 복구 Lv5: 야외 이동 불가 → 마을로 이동")
                            return move
                    # All named locations exhausted — desperate exploration
                    _intent("교착 복구 Lv5: 탐색 실패 → 랜덤 방향 원거리 탐색")
                    return _DesperateExploration(ss)

                # Level 6: Stationary yell for help — no pathfinding,
                # so it survives the common failure mode where every
                # named destination is in `_failed_destinations` and
                # every move returns blocked.  Runs before the forum
                # escalation so we try one more in-game visibility pass
                # (the only strategy that empirically succeeded in the
                # observed deadlock) before the 5-minute silent wait.
                elif _deadlock_level == 6:
                    _intent("교착 복구 Lv6: 제자리에서 도움 외치기")
                    return _YellForHelp(ss)

                # Level 7+: Forum escalation, then reset cycle
                if _deadlock_level >= 7:
                    ctx.blackboard["_deadlock_recovery_level"] = 0
                    # Remember where this cycle burned itself out so the
                    # next entry can short-circuit to Lv4 if we're still
                    # sitting in the same dead area.
                    if _first_pos is not None:
                        ctx.blackboard["_deadlock_last_cycle_pos"] = _first_pos
                    ctx.blackboard["_deadlock_cycles_completed"] = (
                        ctx.blackboard.get("_deadlock_cycles_completed", 0) + 1
                    )
                    ctx.blackboard.pop("_deadlock_first_pos", None)
                    # Fresh cycle — re-enable begging.  If the relocate
                    # at Lv4 moved us to a new town, different NPCs are in
                    # range and might actually give coin.  Same argument
                    # for hunting: a new town has fresh huntable spawns.
                    ctx.blackboard.pop("_beg_consecutive_fails", None)
                    ctx.blackboard.pop("_hunt_consecutive_fails", None)
                    _intent("교착 상태 Lv7: 포럼에 도움 요청")
                    await self._deadlock.escalate_to_forum(ctx)
                    return None

            # Try to find valuable items on the ground nearby
            ground_items = self._deadlock.find_ground_valuables(ctx, ss)
            if ground_items:
                _intent(f"교착 복구: 바닥에 아이템 {len(ground_items)}개 발견 → 줍기")
                return _ScavengeGroundItems(ground_items, ss)

            # Loot nearby corpses — passive income that requires no
            # combat skill or tools.  Guards, other players, and NPCs
            # kill monsters whose corpses may contain gold and items.
            corpses = [
                it for it in ctx.perception.world.items.values()
                if (it.container == 0
                    and it.graphic == 0x2006
                    and max(abs(it.x - ss.x), abs(it.y - ss.y)) <= 18
                    and it.serial not in
                    ctx.blackboard.get("_looted_corpses", set()))
            ]
            if corpses:
                _intent(f"교착 복구: 시체 {len(corpses)}개 발견 → 루팅")
                return _LootCorpses(ss)

            # Hunt nearby creatures for gold — at level 2+ include small
            # animals (rats drop 1-5gp, enough to bootstrap a pickaxe).
            if _deadlock_level >= 2:
                target = self._find_huntable_target(
                    ctx, ss, include_small_animals=True,
                )
                if target:
                    _intent(f"교착 복구: {target.name or 'creature'} 사냥 → 금화 확보")
                    return _HuntForGold(target, ss)

            # Walk toward useful area — at level 3+ try wilderness where
            # monsters spawn; otherwise try populated town areas.
            if time.time() > self._move_fail_until:
                if _deadlock_level >= 3:
                    # Try wilderness first (monsters spawn near mines/camps)
                    move = await self._roaming.move_to_location(
                        ctx, "mine", "mining", "camp",
                    )
                    if move:
                        _intent("교착 복구: 사냥감 탐색 → 야외로 이동")
                        return move
                    # Wilderness blocked — try visible vendor NPC directly
                    vendor_npc = self._find_visible_vendor_npc(ctx, ss)
                    if vendor_npc:
                        _intent(f"교착 복구: 근처 상인 {vendor_npc.name or 'NPC'} 방문")
                        return _MoveToProcedure(
                            vendor_npc.name or "vendor",
                            vendor_npc.x, vendor_npc.y,
                        )
                    # No visible vendors — try town locations as last resort
                    move = await self._roaming.move_to_location(
                        ctx, "bank", "tavern", "inn", "guild", "road",
                    )
                    if move:
                        _intent("교착 복구: 마을 중심부로 이동 → 상인 탐색")
                        return move
                else:
                    move = await self._roaming.move_to_location(
                        ctx, "bank", "tavern", "inn", "blacksmith",
                    )
                    if move:
                        _intent("교착 복구: 주변에 아이템 없음 → 마을로 이동")
                        return move

            # All immediate paths exhausted at Level 4+ — try desperate
            # exploration (random walk in new direction) before giving up.
            if _deadlock_level >= 4:
                _intent("교착 복구: 모든 경로 차단 → 랜덤 방향 탐색")
                return _DesperateExploration(ss)

            # Level 2 with a weapon: before random-wandering the town yet
            # again, walk toward a wilderness spawn area (mine/camp) so
            # `_find_huntable_target` has something to bite on next tick.
            # Town wandering rarely surfaces gold-dropping creatures; a
            # dagger-armed agent's best bootstrap is rats/mongbats at the
            # mine edges.  Gated on _has_usable_weapon so unarmed agents
            # still fall through to the safer town-wander path below.
            if (
                _deadlock_level == 2
                and self._has_usable_weapon(ctx, ss)
                and time.time() > self._move_fail_until
            ):
                move = await self._roaming.move_to_location(
                    ctx, "mine", "mining", "camp",
                )
                if move:
                    _intent("교착 복구 Lv2: 무기 보유 → 야외 사냥터로 이동")
                    return move

            # Levels 2-3: hunting returned None (all targets unreachable
            # or blacklisted) and movement is blocked.  Don't just return
            # None and waste 3 attempts — wander to discover new terrain.
            if _deadlock_level >= 2:
                _intent("교착 복구: 사냥/이동 불가 → 마을 주변 탐색")
                return _WanderAndScavenge(ss)

            # Level 0-1: return None; attempt counter was already
            # incremented so we'll escalate after 3 idle entries.
            _intent("교착 상태: 복구 시도 중 → 다음 레벨로 에스컬레이션 대기")
            return None

        # Made it past Priority 4 (have mining tools) → reset deadlock state
        ctx.blackboard.pop("_deadlock_recovery_level", None)
        ctx.blackboard.pop("_deadlock_attempt_count", None)
        ctx.blackboard.pop("_deadlock_first_pos", None)
        ctx.blackboard.pop("_deadlock_cycles_completed", None)
        ctx.blackboard.pop("_deadlock_last_cycle_pos", None)
        ctx.blackboard.pop("_beg_consecutive_fails", None)
        ctx.blackboard.pop("_hunt_consecutive_fails", None)

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
                            "cycles": expedition.cycles_completed,
                            "duration_s": duration,
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
            move = await self._roaming.move_to_location(ctx, "bank")
            if move is not None:
                return move
            # No bank reachable right now. A large gold pile has no in-pack
            # use, so hoarding it stays the priority — return idle and wait
            # for a bank to become reachable. But colored ingots are a passive
            # byproduct of ordinary mining (ore spawns at random hues): if the
            # ONLY reason we entered this branch is a stray colored stack, a
            # blocked bank must NOT idle a tool-equipped miner who could keep
            # earning. Fall through to the mining loop (Priority 7) and carry
            # the ingots until a bank is reachable, instead of going dark.
            if ss.gold > 200:
                _intent(f"금화 {ss.gold}g 보유, 은행 도달 불가 → 대기")
                return None
            logger.info(
                "planner_colored_ingot_bank_unreachable_continue",
                pos=f"({ss.x},{ss.y})",
            )

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
    # Anti-freeze: liveness watchdog + forced fallback activity
    # ------------------------------------------------------------------

    async def _liveness_watchdog(self, ctx: AgentContext) -> None:
        """Cancel a stalled procedure and force fallback activity.

        Runs as a separate task so it fires even while tick() is blocked
        inside a frozen procedure — the same-task _check_stuck() cannot
        do that. Progress = a successful result (recorded in _run_loop)
        or any position change.
        """
        last_pos: tuple[int, int] | None = None
        while self._running and ctx.conn.connected:
            await asyncio.sleep(self._WATCHDOG_POLL_S)
            if self.command_bus and self.command_bus.paused:
                self._last_progress_ts = _time.monotonic()
                continue
            # The watchdog is the planner's last line of defense against a
            # freeze: it runs as a detached, never-awaited task, so an
            # unhandled exception here would kill it *silently* and leave the
            # agent able to stall undetected for the rest of the session
            # (crushing the liveness/fitness metrics). A transient perception
            # glitch, a bus publish error, etc. must not tear the watchdog
            # down. The asyncio.sleep at the top of the loop also keeps a
            # persistent fault from busy-spinning. A real shutdown
            # (CancelledError) still propagates.
            try:
                ss = ctx.perception.self_state
                if (ss.x, ss.y) != last_pos:
                    last_pos = (ss.x, ss.y)
                    self._last_progress_ts = _time.monotonic()
                stalled_s = _time.monotonic() - self._last_progress_ts
                if stalled_s <= self._WATCHDOG_STALL_S:
                    continue

                logger.warning(
                    "planner_watchdog_stall",
                    stalled_s=f"{stalled_s:.0f}",
                    procedure=self._last_procedure,
                )
                task = self._current_proc_task
                cancelled_running = False
                if task is not None and not task.done():
                    self._watchdog_cancelled = True
                    task.cancel()
                    cancelled_running = True
                # Only blame the procedure if we actually killed one mid-run.
                # An idle stall (nothing selectable) is not its fault — the
                # forced fallback below is the remedy there.
                if cancelled_running and self._last_procedure:
                    self._proc_breaker.record_failure(self._last_procedure)
                self._force_fallback_until = _time.time() + self._FALLBACK_WINDOW_S
                self._last_progress_ts = _time.monotonic()  # re-arm
                if ctx.bus:
                    ctx.bus.publish("system.stuck", {
                        "message": (
                            f"Watchdog: no progress for {stalled_s:.0f}s — "
                            f"forcing fallback activity"
                        ),
                        "importance": 2,
                    })
            except asyncio.CancelledError:
                raise  # real shutdown — propagate
            except Exception as e:
                logger.warning("planner_watchdog_error", error=str(e))

    async def _fallback_procedure(self, ctx: AgentContext):
        """Pick a guaranteed-liveness activity for the fallback window.

        Walking always emits wire actions — exactly what the kernel's
        liveness gate measures — so the last resort is a short walk.
        """
        import random

        stalled = self._last_procedure
        # 1) primary profession work, if it can actually start
        for name in ("mine_ore", "chop_wood"):
            if name == stalled:
                continue
            proc = self.registry.get(name)
            if proc is None:
                continue
            try:
                if await proc.can_start(ctx):
                    return proc
            except Exception:
                continue
        # 2) walk toward a known activity location
        try:
            proc = await self._roaming.try_move_to_activity(ctx)
            if proc is not None and getattr(proc, "name", "") != stalled:
                return proc
        except Exception:
            pass
        # 3) last resort: short random walk
        ss = ctx.perception.self_state
        dx = random.choice([-15, -12, -10, 10, 12, 15])
        dy = random.choice([-15, -12, -10, 10, 12, 15])
        return _MoveToProcedure("anti_freeze_wander", ss.x + dx, ss.y + dy)

    # ------------------------------------------------------------------
    # Stuck / deadlock detection and resolution
    # ------------------------------------------------------------------

    # Thresholds (in ticks — 1 tick ≈ 200ms + procedure time)
    _IDLE_WARN = 50       # ~10s of no procedure → log warning
    _IDLE_ESCALATE = 150  # ~30s → try deadlock resolution
    _IDLE_FORUM = 600     # ~2min → post to forum for help + pause
    _REPEAT_FAIL_LIMIT = 10  # same procedure failing 10x → stop trying

    def _is_productive_grind(
        self,
        dominant: str | None,
        last_result: "ProcedureResult | None",
    ) -> bool:
        """Decide whether a low-diversity window is a productive grind.

        The health watchdog flags any low-diversity selection window as a
        loop. A *productive* mono-procedure grind (hunt_nearby landing kills,
        a practice loop gaining skill) repeats by design and must NOT eat the
        60s health break — but a *stuck* loop (the dominant procedure failing
        over and over) must.

        The previous test ``repeat_counter[dominant] == 0`` was wrong: that
        counter is only updated on the tick a procedure actually ran, so a
        stale ``0`` survives across many idle/thrash ticks where ``dominant``
        has not been re-run. A window that is thrashing between a *failing*
        procedure and a fallback could keep the dominant's stale counter at 0
        and suppress the break forever. The live signal — whether the most
        recent tick actually produced a successful result — is authoritative:
        if the latest run failed (or nothing ran), this is a stuck loop, not a
        productive grind, regardless of the stale per-procedure counter.
        """
        if not dominant:
            return False
        # Most recent tick must have succeeded — the freshest evidence that
        # the grind is making progress rather than spinning on failures.
        if last_result is None or not last_result.success:
            return False
        # And the dominant procedure must not be carrying consecutive
        # failures (belt-and-suspenders: a success on a *different* procedure
        # should not exempt a dominant one that keeps failing).
        return self._repeat_counter.get(dominant, 0) == 0

    async def _check_stuck(self, ctx: AgentContext) -> None:
        """Detect stuck loops and deadlocks, escalate progressively."""
        import time as _time

        # If a PlannerHealth break is in progress the planner is *supposed*
        # to be idle — don't let idle-tick escalation fire during the break
        # (it would race the health break and reset its own counters).
        if _time.time() < self._health_break_until:
            return

        # --- Idle detection: planner returning None repeatedly ---
        #
        # Escalate on a >= *crossing*, not an exact == match. _idle_ticks is
        # incremented in _run_loop one tick before _check_stuck runs, but a
        # tick can advance the counter without _check_stuck observing that
        # exact value: the health-break early-return above skips a tick, and
        # any exception earlier in the loop body skips the _check_stuck call
        # while the increment already happened. With the old `== threshold`
        # test, a single skipped observation at the threshold value meant the
        # deadlock resolver / forum escalation NEVER fired and the agent sat
        # idle for the rest of the session. Crossing detection + a per-run
        # watermark fires each rung exactly once and is robust to skips.
        if self._idle_ticks < self._IDLE_WARN:
            # Counter reset (a procedure ran) — re-arm all rungs for the next
            # idle run.
            self._idle_escalation_fired = 0
            return

        if (self._idle_ticks >= self._IDLE_FORUM
                and self._idle_escalation_fired < self._IDLE_FORUM):
            self._idle_escalation_fired = self._IDLE_FORUM
            await self._deadlock.escalate_to_forum(ctx)
        elif (self._idle_ticks >= self._IDLE_ESCALATE
                and self._idle_escalation_fired < self._IDLE_ESCALATE):
            self._idle_escalation_fired = self._IDLE_ESCALATE
            await self._deadlock.resolve(ctx)
        elif (self._idle_ticks >= self._IDLE_WARN
                and self._idle_escalation_fired < self._IDLE_WARN):
            self._idle_escalation_fired = self._IDLE_WARN
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

    def _sellable_graphics(self, ctx: AgentContext, ss) -> set[int]:
        """Graphics in backpack the agent can sell to a vendor.

        Includes non-KEEP items (clothing, books, weapons) plus surplus
        crafting tools (count ≥ 2 of a given graphic) — sell_to_vendor's
        keep-at-least-1 logic protects one serial per tool group, so
        duplicates are safe to offload for gold.  Crucial when the agent
        deadlocks carrying e.g. 4 tinker's tools + 2 tongs: without this,
        the planner only sees dagger/book/candle as sellable and ignores
        the real liquidation opportunity.
        """
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return set()
        from anima.skills.trade.vendor import KEEP_GRAPHICS
        from anima.skills.crafting.tinker import (
            TINKER_TOOLS_GRAPHICS as _TINKER_GFX,
            PICKAXE_GRAPHICS as _PICK_GFX,
            HATCHET_GRAPHICS as _HATCH_GFX,
            SAW_GRAPHICS as _SAW_GFX,
        )
        from anima.procedures.craft_blacksmith import TONGS_GRAPHICS as _TONGS_GFX

        pack_items = [
            it for it in ctx.perception.world.items.values()
            if it.container == backpack
        ]
        graphics: set[int] = {
            it.graphic for it in pack_items if it.graphic not in KEEP_GRAPHICS
        }

        for group in (_TONGS_GFX, _TINKER_GFX, _PICK_GFX, _HATCH_GFX, _SAW_GFX):
            group_items = [it for it in pack_items if it.graphic in group]
            if len(group_items) >= 2:
                graphics.update(it.graphic for it in group_items)
        return graphics

    def _has_sellable_junk(self, ctx: AgentContext, ss) -> bool:
        """True if backpack contains items a vendor would buy.

        Used in deadlock recovery.  See :meth:`_sellable_graphics` for
        what counts (non-KEEP items + surplus duplicate tools).
        """
        return bool(self._sellable_graphics(ctx, ss))

    def _sellable_vendor_keywords(self, ctx: AgentContext, ss) -> list[str]:
        """Return location keywords for vendors that buy the agent's items.

        Uses ITEM_VENDOR_MAP to map actual backpack items → vendor types,
        so the agent walks to the RIGHT vendor (e.g. "arms" for daggers,
        "tailor" for clothing, "tinker" for surplus tinker's tools).
        """
        from anima.procedures.vendor_knowledge import get_vendor_keywords_for_items

        sellable_graphics = self._sellable_graphics(ctx, ss)
        if not sellable_graphics:
            return ["blacksmith", "arms", "provisioner", "tailor"]
        return get_vendor_keywords_for_items(sellable_graphics)

    def _find_huntable_target(
        self, ctx: AgentContext, ss, *,
        include_small_animals: bool = False,
    ):
        """Find a nearby monster the agent can attack for gold loot.

        When *include_small_animals* is True (used in deadlock recovery),
        rats and other small town creatures are included — they drop 1-5
        gold in classic UO and are a viable bootstrap income source.
        """
        from anima.perception.enums import NotorietyFlag

        ATTACKABLE = {
            NotorietyFlag.ATTACKABLE,
            NotorietyFlag.CRIMINAL,
            NotorietyFlag.ENEMY,
            NotorietyFlag.MURDERER,
        }
        HUMAN_BODIES = {0x0190, 0x0191}
        # Small harmless town animals: often behind walls/inside buildings
        # and never drop gold. Skip them to avoid wasting movement budget.
        TOWN_PETS = {0x00C9, 0x00D9, 0x00EE}  # cat, dog, rat

        no_gold_bodies = ctx.blackboard.get("_hunt_no_gold_bodies", set())
        unkillable_bodies = ctx.blackboard.get("_hunt_unkillable_bodies", set())

        # Skip targets we already failed to reach (behind walls, inside
        # buildings).  Entries expire after 5 minutes so the agent retries
        # if the target moves to a reachable position.
        import time as _t
        unreachable: dict[int, float] = ctx.blackboard.get("_hunt_unreachable", {})
        now = _t.time()
        # Lazy cleanup of expired entries
        expired = [s for s, t in unreachable.items() if now - t > 300]
        for s in expired:
            del unreachable[s]

        # Body-type unreachability: when distinct mobiles of the same body
        # have been unreachable repeatedly (e.g. cats that flee faster than
        # the agent can chase), skip the whole body type so deadlock recovery
        # escalates instead of burning ticks on another cat of the same kind.
        # Entries expire after 5 minutes so the agent retries in new terrain.
        unreachable_bodies: dict[int, float] = ctx.blackboard.get(
            "_hunt_unreachable_bodies", {},
        )
        expired_bodies = [b for b, t in unreachable_bodies.items() if now - t > 300]
        for b in expired_bodies:
            del unreachable_bodies[b]

        candidates = []
        for m in ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=18):
            if m.serial == ss.serial:
                continue
            if m.notoriety not in ATTACKABLE:
                continue
            # Don't attack humans unless clearly hostile
            if m.body in HUMAN_BODIES and m.notoriety == NotorietyFlag.ATTACKABLE:
                continue
            # Skip invulnerable / blessed mobiles (yellow health bar). ServUO
            # marks Healers and protected town NPCs with the YELLOW_BAR flag
            # (MobileInfo.is_yellow_health) — they take zero damage however long
            # we swing, yet can read as gray/ATTACKABLE notoriety. combat_loop.
            # _find_target / _adjacent_hostiles and the survival flee gate
            # (_is_live_hostile) all already skip them; this hunt scan must
            # mirror that or deadlock-recovery hunting locks onto the immortal
            # survival-arena Healer co-located at the resurrection spot (kernel
            # K1+/K1/K3), swinging forever for zero gold and never escalating.
            # `is True` (not truthy) so SimpleNamespace test mobs lacking the
            # field aren't mis-excluded.
            if getattr(m, "is_yellow_health", False) is True:
                continue
            # Skip small town animals unless deadlock-hunting for gold
            if m.body in TOWN_PETS and not include_small_animals:
                continue
            # Skip creature types we've killed before that dropped no gold
            if m.body in no_gold_bodies:
                continue
            # Skip body types we repeatedly failed to kill (combat timeout)
            if m.body in unkillable_bodies:
                continue
            # Skip body types we repeatedly failed to reach (pathfinding dead-end)
            if m.body in unreachable_bodies:
                continue
            # Skip targets we failed to reach recently
            if m.serial in unreachable:
                continue
            candidates.append(m)

        if not candidates:
            return None

        # Prefer closest target, but skip unreachable ones (behind walls,
        # inside buildings) to avoid wasting go_to budget on hopeless targets.
        candidates.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))

        if ctx.map_reader:
            from anima.pathfinding import find_path

            for c in candidates:
                quick_path = find_path(
                    ctx.map_reader, ss.x, ss.y, c.x, c.y,
                    max_steps=500,
                    current_z=ss.z,
                    adjacent=True,
                )
                if quick_path:
                    return c
            return None  # none reachable

        return candidates[0]

    def _find_visible_vendor_npc(self, ctx: AgentContext, ss):
        """Find a vendor NPC visible in the world state worth walking toward.

        Unlike _find_vendor (8-tile interaction range), this scans the full
        18-tile visibility range.  Used in deadlock recovery when named-
        location movement is blocked but a vendor NPC is actually visible.
        """
        from anima.skills.trade.vendor import (
            HUMAN_BODIES,
            _is_refused,
            _is_vendor,
        )

        candidates = []
        for m in ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=18):
            if m.serial == ss.serial:
                continue
            if m.body not in HUMAN_BODIES:
                continue
            if _is_refused(ctx, m.serial):
                continue
            if not _is_vendor(m):
                continue
            dist = max(abs(m.x - ss.x), abs(m.y - ss.y))
            if dist <= 2:
                continue  # already next to this vendor
            candidates.append((m, dist))

        if not candidates:
            return None

        # Prefer closest reachable vendor
        candidates.sort(key=lambda pair: pair[1])

        if ctx.map_reader:
            from anima.pathfinding import find_path

            for m, _d in candidates:
                quick_path = find_path(
                    ctx.map_reader, ss.x, ss.y, m.x, m.y,
                    max_steps=500,
                    current_z=ss.z,
                    adjacent=True,
                )
                if quick_path:
                    return m
            return None  # none reachable

        return candidates[0][0]

    def _find_beg_npc(self, ctx: AgentContext, ss):
        """Find the nearest human NPC eligible for the Begging skill.

        Returns the closest visible mobile with a human body that has not
        been begged from in the last _BegNpcForCoin.COOLDOWN_S seconds.
        Used by the deadlock ladder's Lv0 zero-precondition gold path.
        """
        from anima.skills.trade.vendor import HUMAN_BODIES

        begged = ctx.blackboard.get("_begged_npcs", {})
        cooldown = _BegNpcForCoin.COOLDOWN_S
        now = _time.time()

        best = None
        best_dist = 999
        for m in ctx.perception.world.nearby_mobiles(
            ss.x, ss.y, distance=10,
        ):
            if m.serial == ss.serial:
                continue
            if m.body not in HUMAN_BODIES:
                continue
            last = begged.get(m.serial, 0.0)
            if now - last < cooldown:
                continue
            dist = max(abs(m.x - ss.x), abs(m.y - ss.y))
            # Skip targets already on top of us — still fine to beg, but
            # prefer slightly-away NPCs so movement doesn't short-circuit
            if dist < best_dist:
                best = m
                best_dist = dist
        return best

    def _has_hatchet(self, ctx: AgentContext, ss) -> bool:
        """True if the agent has a hatchet in backpack or equipped."""
        from anima.skills.gathering.lumber import HATCHET_GRAPHICS

        backpack = ss.equipment.get(0x15)
        if not backpack:
            return False
        for slot in (0x01, 0x02):
            wep = ss.equipment.get(slot)
            if wep:
                it = ctx.perception.world.items.get(wep)
                if it and it.graphic in HATCHET_GRAPHICS:
                    return True
        for it in ctx.perception.world.items.values():
            if it.container == backpack and it.graphic in HATCHET_GRAPHICS:
                return True
        return False

    def _has_usable_weapon(self, ctx: AgentContext, ss) -> bool:
        """True if the agent carries/wears a weapon `_HuntForGold` can equip.

        Used by the Lv2 wilderness-seek branch — walking to a hunt-area
        is only useful if we can actually fight what we find there.
        """
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return False
        weapons = _HuntForGold._WEAPON_GRAPHICS
        # Already wielding something? any equipped weapon (slots 1/2) counts.
        for slot in (0x01, 0x02):
            wep = ss.equipment.get(slot)
            if wep:
                for it in ctx.perception.world.items.values():
                    if it.serial == wep and it.graphic in weapons:
                        return True
        # Otherwise scan backpack contents
        for it in ctx.perception.world.items.values():
            if it.container == backpack and it.graphic in weapons:
                return True
        return False


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
        # Daggers — the agent's most common starting weapon; needed for
        # hunt_for_gold combat.  Dropping them before Level 5 makes
        # hunting futile.
        DAGGER_GRAPHICS = {0x0F51, 0x0F52}

        # Keep items useful for the mining/crafting loop + combat
        keep = (
            ORE_GRAPHICS
            | INGOT_GRAPHICS
            | PICKAXE_GRAPHICS
            | SHOVEL_GRAPHICS
            | TINKER_TOOLS_GRAPHICS
            | TONGS_GRAPHICS
            | DAGGER_GRAPHICS
            | {GOLD_GRAPHIC, BANDAGE_GRAPHIC}
        )

        # Blessed/newbified items silently reject server-side (0x27) — once
        # a serial fails to drop, never try it again.
        undroppable: set[int] = ctx.blackboard.setdefault(
            "undroppable_serials", set(),
        )

        droppable = [
            it for it in ctx.perception.world.items.values()
            if it.container == backpack
            and it.graphic not in keep
            and it.serial not in undroppable
        ]

        if not droppable:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no droppable items in backpack",
            )

        dropped = 0
        undroppable_new: list[int] = []
        world_items = ctx.perception.world.items
        for item in droppable[:10]:
            # Drop to ground at current position (container=0 → ground)
            await ctx.conn.send_packet(
                build_drop_item(item.serial, x=ss.x, y=ss.y, z=ss.z)
            )
            await asyncio.sleep(0.4)
            # Verify the server actually removed the item. Blessed items
            # stay in backpack — server silently replies 0x27 (Reject Move
            # Item). Without verification we'd report success and the
            # Lv0→Lv1 escalation would clear refused_vendors expecting a
            # changed inventory, trapping the agent in a sell-loop.
            still_here = world_items.get(item.serial)
            if still_here is not None and still_here.container == backpack:
                undroppable.add(item.serial)
                undroppable_new.append(item.serial)
                logger.warning(
                    "drop_junk_rejected",
                    serial=f"0x{item.serial:08X}",
                    graphic=f"0x{item.graphic:04X}",
                    reason="item remained in backpack after drop — likely blessed",
                )
                continue
            dropped += 1
            logger.info(
                "dropped_junk_item",
                serial=f"0x{item.serial:08X}",
                graphic=f"0x{item.graphic:04X}",
                amount=item.amount,
            )

        # Inventory changed only when at least one drop actually took effect.
        # The caller (Lv0→Lv1 escalation) uses this to decide whether to
        # clear refused_vendors — clearing after a no-op drop would
        # re-select the same wrong-type vendor in the next tick.
        if dropped > 0:
            ctx.blackboard.pop("refused_vendors", None)

        if dropped == 0 and undroppable_new:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=(
                    f"All {len(undroppable_new)} candidate items rejected "
                    "by server (blessed/newbified)"
                ),
            )

        return ProcedureResult(
            success=dropped > 0,
            message=f"Dropped {dropped} junk items to free weight",
            reason=None if dropped > 0 else FailureReason.MISSING_RESOURCE,
        )


class _BegAtBank:
    """Deadlock recovery Lv3: walk to a populated area and ask for help.

    Banks and taverns are where players congregate and drop items.
    The agent walks toward visible human NPCs or players, says it
    needs help in-game, then scans for any items that appear on the
    ground (other players may drop spare tools or gold).

    This is realistic UO behavior — Britain Bank is famous for
    charitable item drops and player interaction.
    """

    WAIT_TICKS = 6       # how many 5-second waits (total ~30s)
    TICK_DELAY = 5.0     # seconds between scans while begging
    SCAN_RADIUS = 3      # tiles to check for dropped items

    def __init__(self, ss) -> None:
        self.name = "beg_at_bank"
        self.description = "Ask nearby players for help"
        self._start_pos = (ss.x, ss.y)

    async def can_start(self, ctx) -> bool:
        return True

    async def run(self, ctx) -> ProcedureResult:
        import asyncio

        from anima.action.movement import go_to
        from anima.client.packets import (
            build_drop_item,
            build_pick_up,
            build_unicode_speech,
        )

        ss = ctx.perception.self_state
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no backpack",
            )

        # Walk toward nearest human NPC cluster (players gather near them)
        HUMAN_BODIES = {0x0190, 0x0191}
        humans = [
            m for m in ctx.perception.world.nearby_mobiles(
                ss.x, ss.y, distance=18,
            )
            if m.serial != ss.serial and m.body in HUMAN_BODIES
        ]
        if humans:
            # Walk toward the closest human
            humans.sort(
                key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y),
            )
            target = humans[0]
            dist = max(abs(target.x - ss.x), abs(target.y - ss.y))
            if dist > 3:
                await go_to(ctx, target.x, target.y)

        # Say we need help
        ss = ctx.perception.self_state
        try:
            await ctx.conn.send_packet(build_unicode_speech(
                "Hail! I'm a miner down on my luck. "
                "Lost all my tools and coin. "
                "Could anyone spare a pickaxe or a few gold?"
            ))
            await asyncio.sleep(2.0)
            await ctx.conn.send_packet(build_unicode_speech(
                "I'll repay the favor in ingots "
                "once I'm back on my feet."
            ))
        except Exception:
            pass

        logger.info(
            "beg_at_bank_waiting",
            pos=f"({ss.x},{ss.y})",
            nearby_humans=len(humans),
        )

        # Wait and scan for items appearing on ground
        items_picked = 0
        gold_received = False

        for _ in range(self.WAIT_TICKS):
            await asyncio.sleep(self.TICK_DELAY)

            ss = ctx.perception.self_state

            # Check if someone gave us gold directly
            if ss.gold >= 10:
                gold_received = True
                logger.info("beg_received_gold", gold=ss.gold)
                break

            # Check if someone gave us a tool
            from anima.actions.inventory import find_in_backpack
            from anima.skills.gathering.mine import PICKAXE_GRAPHICS
            if find_in_backpack(ctx, PICKAXE_GRAPHICS | {0x0F39}):
                logger.info("beg_received_tool")
                return ProcedureResult(
                    success=True,
                    message="Received a mining tool from another player!",
                )

            # Scan for items on ground within pickup range
            for it in ctx.perception.world.items.values():
                if (it.container == 0
                        and max(abs(it.x - ss.x), abs(it.y - ss.y))
                        <= self.SCAN_RADIUS):
                    if ss.weight_max > 0 and ss.weight > ss.weight_max - 50:
                        break
                    dist = max(abs(it.x - ss.x), abs(it.y - ss.y))
                    if dist > 2:
                        arrived = await go_to(ctx, it.x, it.y)
                        if not arrived:
                            continue
                    await ctx.conn.send_packet(
                        build_pick_up(it.serial, it.amount)
                    )
                    await asyncio.sleep(0.3)
                    await ctx.conn.send_packet(
                        build_drop_item(it.serial, container=backpack)
                    )
                    await asyncio.sleep(0.3)
                    items_picked += 1
                    logger.info(
                        "beg_picked_item",
                        serial=f"0x{it.serial:08X}",
                        graphic=f"0x{it.graphic:04X}",
                    )

            if items_picked >= 3:
                break

        if gold_received or items_picked > 0:
            parts = []
            if gold_received:
                parts.append(f"received {ss.gold}gp")
            if items_picked:
                parts.append(f"picked up {items_picked} items")
            return ProcedureResult(
                success=True,
                message=f"Begged at bank: {', '.join(parts)}",
            )

        return ProcedureResult(
            success=False,
            reason=FailureReason.MISSING_RESOURCE,
            message="Begged at bank but no one helped",
        )


class _BegNpcForCoin:
    """Deadlock recovery Lv0: use UO Begging skill on a nearby NPC.

    UO's Begging skill (ID 6) is a zero-precondition bootstrap gold
    source: send UseSkill(6), target an NPC, and the server awards 1-5gp
    on success (or nothing + karma loss on failure).  Unlike the other
    beg/yell procedures, this doesn't require other players to be online
    — NPCs are always around.  Intended to fund a ~15gp pickaxe within
    a handful of attempts so the agent can exit the true-deadlock state
    without needing combat capability, sellable inventory, or relocation.

    Per-target cooldown lives in ``ctx.blackboard["_begged_npcs"]`` and
    is stamped by this procedure before the skill fires — regardless of
    outcome we avoid re-begging the same NPC during its recharge window.
    """

    SKILL_ID = 6
    COOLDOWN_S = 30.0
    APPROACH_DIST = 3
    # After this many consecutive procedure-level failures (no cursor,
    # refusal, blocked approach), the planner stops electing beg and lets
    # the deadlock ladder escalate past Lv0.  Cleared on successful begs
    # and on any gold progress (see `deadlock_gold_progress` branch).
    MAX_CONSECUTIVE_FAILS = 3

    def __init__(self, ss, target) -> None:
        self.name = "beg_npc_for_coin"
        self.description = "Use Begging skill on a nearby NPC"
        self._start_pos = (ss.x, ss.y)
        self._target = target

    async def can_start(self, ctx) -> bool:
        return True

    async def run(self, ctx) -> ProcedureResult:
        import asyncio

        from anima.action.movement import go_to
        from anima.actions.target import target_object, wait_for_target
        from anima.client.packets import build_use_skill

        ss = ctx.perception.self_state
        target = self._target
        start_gold = ss.gold

        def _record_failure(reason: FailureReason, message: str) -> ProcedureResult:
            ctx.blackboard["_beg_consecutive_fails"] = (
                ctx.blackboard.get("_beg_consecutive_fails", 0) + 1
            )
            return ProcedureResult(
                success=False, reason=reason, message=message,
            )

        dist = max(abs(target.x - ss.x), abs(target.y - ss.y))
        if dist > self.APPROACH_DIST:
            await go_to(ctx, target.x, target.y)
            ss = ctx.perception.self_state
            dist = max(abs(target.x - ss.x), abs(target.y - ss.y))
            if dist > self.APPROACH_DIST + 1:
                return _record_failure(
                    FailureReason.BLOCKED,
                    f"Could not approach {target.name or 'NPC'}",
                )

        # Stamp cooldown up front so a hang/timeout doesn't re-elect the
        # same NPC next tick.
        begged = ctx.blackboard.setdefault("_begged_npcs", {})
        begged[target.serial] = _time.time()

        ss.pending_target = None
        try:
            await ctx.conn.send_packet(build_use_skill(self.SKILL_ID))
        except Exception as e:
            return _record_failure(
                FailureReason.BLOCKED,
                f"UseSkill(Begging) failed: {e}",
            )

        cursor = await wait_for_target(ctx, timeout=3.0)
        if not cursor.success:
            logger.info(
                "beg_npc_no_cursor",
                npc=target.name or f"0x{target.serial:08X}",
            )
            return _record_failure(
                FailureReason.BLOCKED,
                "Begging skill — no target cursor",
            )

        cursor_id = cursor.data.get("cursor_id", 0) if cursor.data else 0
        await target_object(ctx, cursor_id, target.serial)

        # Wait briefly for the server to award gold or refuse.
        for _ in range(6):  # 6 × 0.5s = 3s
            await asyncio.sleep(0.5)
            ss = ctx.perception.self_state
            if ss.gold > start_gold:
                gained = ss.gold - start_gold
                logger.info(
                    "beg_npc_received_gold",
                    npc=target.name or f"0x{target.serial:08X}",
                    gained=gained,
                )
                ctx.blackboard["_beg_consecutive_fails"] = 0
                return ProcedureResult(
                    success=True,
                    message=(
                        f"Begged {gained}gp from {target.name or 'NPC'}"
                    ),
                )

        return _record_failure(
            FailureReason.MISSING_RESOURCE,
            f"{target.name or 'NPC'} refused to give coin",
        )


class _YellForHelp:
    """Deadlock recovery Lv6: stationary sustained shouts for help.

    When all movement-based recoveries (wander, relocate, hunt) have
    failed — often because every named destination is in
    ``_failed_destinations`` and pathfinding produces no route — the
    agent is effectively pinned in place.  Walking to another beg spot
    isn't possible and forum escalation costs a 5-minute silent wait.

    This procedure fills that gap: stand still, shout in Unicode every
    ~12 seconds for ~75 seconds, and pick up anything a passing player
    drops within immediate reach.  No pathfinding, no named destinations
    — so it can't be blocked by stale destination blocklists.  The
    observed success case (`beg_at_bank`) already proves the agent can
    receive items from nearby players when it makes itself visible; this
    is the same primitive, stripped of the walk-toward-humans step that
    the blocked pathfinder keeps tripping over.
    """

    WAIT_TICKS = 25       # 25 × 3s = 75s total
    TICK_DELAY = 3.0
    SHOUT_EVERY = 4       # shout every 4 ticks ≈ 12s
    SCAN_RADIUS = 2       # pick up items within 2 tiles (immediate reach)

    _SHOUTS = (
        "Help! I am stranded with no tools and no coin.",
        "A pickaxe or a handful of gold would save me — I'll repay in ingots.",
        "Please, any kind soul — I cannot leave this spot.",
    )

    def __init__(self, ss) -> None:
        self.name = "yell_for_help"
        self.description = "Stationary shouting for help (no pathfinding)"
        self._start_pos = (ss.x, ss.y)

    async def can_start(self, ctx) -> bool:
        return True

    async def run(self, ctx) -> ProcedureResult:
        import asyncio

        from anima.client.packets import (
            build_drop_item,
            build_pick_up,
            build_unicode_speech,
        )

        ss = ctx.perception.self_state
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no backpack",
            )

        logger.info(
            "yell_for_help_start",
            pos=f"({ss.x},{ss.y})",
            duration_s=int(self.WAIT_TICKS * self.TICK_DELAY),
        )

        items_picked = 0
        gold_received = False
        shout_idx = 0

        for tick in range(self.WAIT_TICKS):
            if tick % self.SHOUT_EVERY == 0:
                try:
                    await ctx.conn.send_packet(
                        build_unicode_speech(
                            self._SHOUTS[shout_idx % len(self._SHOUTS)]
                        )
                    )
                    shout_idx += 1
                except Exception:
                    pass

            await asyncio.sleep(self.TICK_DELAY)

            ss = ctx.perception.self_state

            if ss.gold >= 10:
                gold_received = True
                logger.info("yell_received_gold", gold=ss.gold)
                break

            from anima.actions.inventory import find_in_backpack
            from anima.skills.gathering.mine import PICKAXE_GRAPHICS
            if find_in_backpack(ctx, PICKAXE_GRAPHICS | {0x0F39}):
                logger.info("yell_received_tool")
                return ProcedureResult(
                    success=True,
                    message="Received a mining tool while yelling for help",
                )

            for it in ctx.perception.world.items.values():
                if (it.container == 0
                        and max(abs(it.x - ss.x), abs(it.y - ss.y))
                        <= self.SCAN_RADIUS):
                    if ss.weight_max > 0 and ss.weight > ss.weight_max - 50:
                        break
                    try:
                        await ctx.conn.send_packet(
                            build_pick_up(it.serial, it.amount)
                        )
                        await asyncio.sleep(0.3)
                        await ctx.conn.send_packet(
                            build_drop_item(it.serial, container=backpack)
                        )
                        await asyncio.sleep(0.3)
                        items_picked += 1
                        logger.info(
                            "yell_picked_item",
                            serial=f"0x{it.serial:08X}",
                            graphic=f"0x{it.graphic:04X}",
                        )
                    except Exception:
                        continue

            if items_picked >= 3:
                break

        if gold_received or items_picked > 0:
            parts = []
            if gold_received:
                parts.append(f"received {ss.gold}gp")
            if items_picked:
                parts.append(f"picked up {items_picked} items")
            return ProcedureResult(
                success=True,
                message=f"Yelled for help: {', '.join(parts)}",
            )

        return ProcedureResult(
            success=False,
            reason=FailureReason.MISSING_RESOURCE,
            message="Yelled for help but no one answered",
        )


class _RelocateToCity:
    """Deadlock recovery Lv4: walk to a different city entirely.

    When local town options are exhausted (vendors refuse items, nothing
    on the ground, no huntable monsters), relocate to Britain — the most
    populated city with the widest vendor variety and highest player
    traffic.  Britain Bank is a popular trading spot where items are
    often left on the ground.

    Uses waypoint-style navigation: first walks toward the general
    direction, relying on the planner's next tick to continue or sell
    once vendors are nearby.
    """

    # Britain Bank — most trafficked area in UO
    _TARGET_NAME = "West Britain Bank"
    _TARGET_X = 1425
    _TARGET_Y = 1690

    def __init__(self, ss) -> None:
        self.name = "relocate_to_city"
        self.description = "Relocate to Britain for more options"
        self._start_pos = (ss.x, ss.y)

    async def can_start(self, ctx) -> bool:
        return True

    async def run(self, ctx) -> ProcedureResult:
        from anima.action.movement import go_to

        ss = ctx.perception.self_state

        # If already near Britain, just try selling there
        dist_to_target = max(
            abs(self._TARGET_X - ss.x), abs(self._TARGET_Y - ss.y),
        )
        if dist_to_target <= 30:
            return ProcedureResult(
                success=True,
                message=f"Already near {self._TARGET_NAME}",
            )

        # Walk toward Britain — long distance, use go_to with a
        # waypoint ~100 tiles toward the target to make progress
        # without exhausting the pathfinder's step budget.
        dx = self._TARGET_X - ss.x
        dy = self._TARGET_Y - ss.y
        # Normalize to ~100 tile step
        magnitude = max(abs(dx), abs(dy), 1)
        step = min(100, magnitude)
        wx = ss.x + int(dx * step / magnitude)
        wy = ss.y + int(dy * step / magnitude)

        logger.info(
            "relocate_to_city",
            target=self._TARGET_NAME,
            dist=dist_to_target,
            waypoint=f"({wx},{wy})",
        )

        arrived = await go_to(ctx, wx, wy)

        ss = ctx.perception.self_state
        new_dist = max(
            abs(self._TARGET_X - ss.x), abs(self._TARGET_Y - ss.y),
        )
        progress = dist_to_target - new_dist

        if progress > 10:
            return ProcedureResult(
                success=True,
                message=(
                    f"Relocating to {self._TARGET_NAME}: "
                    f"moved {progress} tiles ({new_dist} remaining)"
                ),
            )

        if not arrived:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"Could not make progress toward {self._TARGET_NAME}",
            )

        return ProcedureResult(
            success=True,
            message=f"Moving toward {self._TARGET_NAME} ({new_dist} tiles remaining)",
        )


class _WanderAndScavenge:
    """Deadlock recovery Lv2: wander around town scanning for useful items.

    Takes random steps through the town area.  At each stop, checks for
    valuable ground items within pickup range and grabs them.  This covers
    areas the agent hasn't visited yet, unlike the stationary scavenge.
    """

    WANDER_STEPS = 20
    STEP_DELAY = 0.6  # seconds between wander steps
    WANDER_RADIUS = 20  # tiles — keep the sweep "near town", never drift away

    def __init__(self, ss) -> None:
        self.name = "wander_and_scavenge"
        self.description = "Wander around town scanning for items"
        self._start_pos = (ss.x, ss.y)

    @classmethod
    def _next_target(cls, start_x, start_y, cur_x, cur_y, rng):
        """Pick the next wander stop: a ±5..15 hop from the current spot,
        clamped so it never strays beyond WANDER_RADIUS of the anchor.

        The old code added a fresh random ±5..15 offset to the *current*
        position every step (target = cur ± rand), so the offsets compounded:
        20 steps could march the agent 100-300 tiles into wilderness/water,
        well past the "±5-15 tiles near town" the docstring promises and far
        from the town items the scavenge exists to grab. Anchoring to the
        start position (like WanderForCombat orbits its combat_anchor) keeps
        the sweep inside the radius it claims to cover.
        """
        dx = rng.choice([-1, 1]) * rng.randint(5, 15)
        dy = rng.choice([-1, 1]) * rng.randint(5, 15)
        target_x = cur_x + dx
        target_y = cur_y + dy
        # Clamp the target back inside the anchor radius. When a hop would
        # leave the box, pin it to the boundary so the next step is pulled
        # back toward town rather than compounding the drift.
        r = cls.WANDER_RADIUS
        target_x = max(start_x - r, min(start_x + r, target_x))
        target_y = max(start_y - r, min(start_y + r, target_y))
        return target_x, target_y

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

        start_x, start_y = self._start_pos
        for _ in range(self.WANDER_STEPS):
            # Pick the next stop near the town anchor (bounded, never drifts)
            ss = ctx.perception.self_state
            target_x, target_y = self._next_target(
                start_x, start_y, ss.x, ss.y, random,
            )

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
        """Find valuable ground items within pickup range of current pos.

        Includes mining items (tools, ore, ingots, gold) AND any item
        that vendors will buy (weapons, clothing, etc.) so the agent
        can sell them for gold during deadlock recovery.
        """
        from anima.procedures.craft_blacksmith import TONGS_GRAPHICS
        from anima.procedures.vendor_knowledge import ITEM_VENDOR_MAP
        from anima.skills.crafting.smelt import INGOT_GRAPHICS
        from anima.skills.crafting.tinker import TINKER_TOOLS_GRAPHICS
        from anima.skills.gathering.mine import ORE_GRAPHICS, PICKAXE_GRAPHICS

        GOLD_GRAPHIC = 0x0EED
        SHOVEL_GRAPHICS = {0x0F39}

        valuable = (
            ORE_GRAPHICS | INGOT_GRAPHICS | PICKAXE_GRAPHICS
            | SHOVEL_GRAPHICS | TINKER_TOOLS_GRAPHICS | TONGS_GRAPHICS
            | {GOLD_GRAPHIC}
            | set(ITEM_VENDOR_MAP.keys())
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
    """Deadlock recovery Lv5: attack a nearby monster and loot gold.

    Walks to the target, enters war mode, fights until target dies or
    timeout, then picks up any gold dropped on the ground.
    """

    COMBAT_TIMEOUT = 30.0
    COMBAT_TICK = 1.0
    # After this many back-to-back procedure-level failures ("cannot reach"
    # or "could not kill"), the pre-ladder opportunistic hunt hook in
    # `_resolve_deadlock` stops re-electing _HuntForGold so the deadlock
    # ladder can escalate past Lv0. Mirrors `_BegNpcForCoin`; cleared on
    # successful kills and on any deadlock gold progress.
    MAX_CONSECUTIVE_FAILS = 3

    def __init__(self, target, ss) -> None:
        self.name = "hunt_for_gold"
        self.description = f"Hunt {target.name or 'monster'} for gold"
        self._target_serial = target.serial
        self._target_name = target.name or "monster"
        self._target_pos = (target.x, target.y)
        self._target_body = target.body

    async def can_start(self, ctx) -> bool:
        return True

    # Weapon graphics the agent can equip for combat (one-handed)
    _WEAPON_GRAPHICS = {
        0x0F51, 0x0F52,  # dagger
        0x0F5E, 0x0F5F,  # broadsword
        0x13FF, 0x1400,  # katana
        0x13B6, 0x13B7,  # scimitar
        0x0F49, 0x0F4A,  # axe
        0x0EC4, 0x0EC5,  # skinning knife
        0x0F61, 0x0F62,  # longsword
        0x1401, 0x1402,  # kryss
        0x13B9, 0x13BA,  # viking sword
        0x0E85, 0x0E86,  # pickaxe (also a weapon)
    }

    async def run(self, ctx) -> ProcedureResult:
        import asyncio
        import time

        from anima.action.movement import go_to
        from anima.client.packets import (
            build_attack,
            build_drop_item,
            build_equip_item,
            build_pick_up,
            build_target_response,
            build_war_mode,
        )

        ss = ctx.perception.self_state

        def _bump_and_return(
            reason: FailureReason, message: str,
        ) -> ProcedureResult:
            # Only called for failure modes that burned real time on this
            # target (unreachable, kill-timeout). "Dead" and "target is
            # gone" are tracked elsewhere and not counted here.
            ctx.blackboard["_hunt_consecutive_fails"] = (
                ctx.blackboard.get("_hunt_consecutive_fails", 0) + 1
            )
            return ProcedureResult(
                success=False, reason=reason, message=message,
            )

        # Bail immediately if dead — can't fight as a ghost
        if not ss.is_alive:
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message="Cannot fight while dead",
            )

        # Cancel any stale targeting cursor left over from a previous
        # action (e.g. double-clicking the dagger triggered "What do you
        # want to use this item on?" instead of equipping).  An
        # unanswered cursor blocks further server interaction.
        if ss.pending_target is not None:
            cursor_id = ss.pending_target.get("cursor_id", 0)
            await ctx.conn.send_packet(build_target_response(
                target_type=0, cursor_id=cursor_id,
            ))
            ss.pending_target = None
            await asyncio.sleep(0.3)

        # Equip a weapon from backpack if not already wielding one.
        # Use the EquipItem packet (0x13) directly instead of
        # double-click, because double-clicking bladed weapons (daggers)
        # opens a targeting cursor instead of equipping.
        backpack = ss.equipment.get(0x15)
        has_weapon_equipped = ss.equipment.get(0x01) or ss.equipment.get(0x02)
        if not has_weapon_equipped and backpack:
            for it in ctx.perception.world.items.values():
                if (it.container == backpack
                        and it.graphic in self._WEAPON_GRAPHICS):
                    await ctx.conn.send_packet(
                        build_equip_item(it.serial, 0x01, ss.serial)
                    )
                    await asyncio.sleep(0.5)
                    logger.info("hunt_equipped_weapon",
                                graphic=f"0x{it.graphic:04X}")
                    break

        # Walk toward target if not adjacent
        dist = max(abs(self._target_pos[0] - ss.x),
                    abs(self._target_pos[1] - ss.y))
        if dist > 1:
            arrived = await go_to(ctx, self._target_pos[0], self._target_pos[1])
            if not arrived:
                # Blacklist this target so _find_huntable_target skips it
                # on subsequent ticks (expires after 5 min).
                import time as _t
                unreachable = ctx.blackboard.setdefault("_hunt_unreachable", {})
                unreachable[self._target_serial] = _t.time()
                # After 2 distinct serials of the same body have been
                # unreachable, blacklist the body type so future selection
                # picks a different species (e.g. cat → rat/dog).  Without
                # this, fast-fleeing prey like cats keep respawning near the
                # agent, each fresh serial passes the per-serial filter, and
                # the opportunistic hunt never escalates.
                if self._target_body:
                    fails: dict[int, set[int]] = ctx.blackboard.setdefault(
                        "_hunt_unreachable_serials_by_body", {},
                    )
                    fails.setdefault(
                        self._target_body, set(),
                    ).add(self._target_serial)
                    if len(fails[self._target_body]) >= 2:
                        ub: dict[int, float] = ctx.blackboard.setdefault(
                            "_hunt_unreachable_bodies", {},
                        )
                        ub[self._target_body] = _t.time()
                        logger.warning(
                            "hunt_unreachable_body",
                            body=hex(self._target_body),
                            name=self._target_name,
                            fails=len(fails[self._target_body]),
                        )
                logger.info(
                    "hunt_target_unreachable",
                    serial=f"0x{self._target_serial:08X}",
                    name=self._target_name,
                )
                return _bump_and_return(
                    FailureReason.BLOCKED,
                    f"Cannot reach {self._target_name}",
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
            # Track body types we repeatedly fail to kill (low combat
            # skill, target too tough, etc.).  After 2 timeouts on the
            # same body type, blacklist it so _find_huntable_target
            # stops wasting 30s per attempt.
            if self._target_body:
                fail_counts: dict[int, int] = ctx.blackboard.setdefault(
                    "_hunt_unkillable_counts", {},
                )
                fail_counts[self._target_body] = fail_counts.get(
                    self._target_body, 0,
                ) + 1
                if fail_counts[self._target_body] >= 2:
                    unkillable: set[int] = ctx.blackboard.setdefault(
                        "_hunt_unkillable_bodies", set(),
                    )
                    unkillable.add(self._target_body)
                    logger.warning(
                        "hunt_unkillable_body",
                        body=hex(self._target_body),
                        name=self._target_name,
                        fails=fail_counts[self._target_body],
                    )
            return _bump_and_return(
                FailureReason.BLOCKED,
                f"Could not kill {self._target_name}",
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

        ctx.blackboard["_hunt_consecutive_fails"] = 0
        return ProcedureResult(
            success=True,
            message=f"Killed {self._target_name}, looted {gold_picked} gold piles",
        )


class _LootCorpses:
    """Deadlock recovery: open nearby corpses and loot gold/items.

    In UO, dead creatures leave corpse containers on the ground
    (graphic 0x2006).  Other players, guards, and NPCs frequently
    kill monsters near town — their corpses may contain gold, weapons,
    or other useful items.  This is a passive income source that
    requires no combat skill or tools.

    The procedure scans for corpses within walk range, double-clicks
    each to open it (server sends 0x3C ContainerContent), then picks
    up gold and valuable items from inside.
    """

    CORPSE_GRAPHIC = 0x2006
    SCAN_RADIUS = 18  # tiles to search for corpses
    MAX_CORPSES = 5   # don't loot more than this per run
    GOLD_GRAPHIC = 0x0EED

    def __init__(self, ss) -> None:
        self.name = "loot_corpses"
        self.description = "Loot nearby corpses for gold and items"

    async def can_start(self, ctx) -> bool:
        return True

    async def run(self, ctx) -> ProcedureResult:
        import asyncio

        from anima.action.movement import go_to
        from anima.client.packets import (
            build_double_click,
            build_drop_item,
            build_pick_up,
        )

        ss = ctx.perception.self_state
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="no backpack",
            )

        # Find corpse items on the ground
        corpses = [
            it for it in ctx.perception.world.items.values()
            if (it.container == 0
                and it.graphic == self.CORPSE_GRAPHIC
                and max(abs(it.x - ss.x), abs(it.y - ss.y))
                <= self.SCAN_RADIUS)
        ]
        if not corpses:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="No corpses nearby",
            )

        # Sort by distance
        corpses.sort(
            key=lambda c: abs(c.x - ss.x) + abs(c.y - ss.y),
        )

        # Skip corpses we already looted (by serial)
        looted_set: set[int] = ctx.blackboard.setdefault(
            "_looted_corpses", set(),
        )

        items_picked = 0
        gold_picked = 0
        corpses_opened = 0

        for corpse in corpses[:self.MAX_CORPSES]:
            if corpse.serial in looted_set:
                continue

            ss = ctx.perception.self_state

            # Walk to corpse if not adjacent
            dist = max(abs(corpse.x - ss.x), abs(corpse.y - ss.y))
            if dist > 2:
                arrived = await go_to(ctx, corpse.x, corpse.y)
                if not arrived:
                    continue

            # Double-click corpse to open it (triggers 0x3C response)
            await ctx.conn.send_packet(build_double_click(corpse.serial))
            await asyncio.sleep(1.5)  # wait for server to send contents
            corpses_opened += 1
            looted_set.add(corpse.serial)

            # Pick up gold and valuable items from inside the corpse
            ss = ctx.perception.self_state
            for it in list(ctx.perception.world.items.values()):
                if it.container != corpse.serial:
                    continue
                # Always pick up gold
                is_gold = it.graphic == self.GOLD_GRAPHIC
                # Also pick up items vendors will buy
                is_valuable = it.graphic in _loot_valuable_graphics()
                if not (is_gold or is_valuable):
                    continue
                if ss.weight_max > 0 and ss.weight > ss.weight_max - 50:
                    break
                await ctx.conn.send_packet(
                    build_pick_up(it.serial, it.amount),
                )
                await asyncio.sleep(0.3)
                await ctx.conn.send_packet(
                    build_drop_item(it.serial, container=backpack),
                )
                await asyncio.sleep(0.3)
                items_picked += 1
                if is_gold:
                    gold_picked += it.amount
                logger.info(
                    "loot_corpse_picked",
                    serial=f"0x{it.serial:08X}",
                    graphic=f"0x{it.graphic:04X}",
                    gold=is_gold,
                )

            if items_picked >= 10:
                break

        # Evict old entries from looted set to prevent unbounded growth
        if len(looted_set) > 200:
            # Keep only the most recent 100 entries (approx — sets
            # aren't ordered, but this caps memory usage)
            to_remove = list(looted_set)[:100]
            for s in to_remove:
                looted_set.discard(s)

        if items_picked > 0:
            return ProcedureResult(
                success=True,
                message=(
                    f"Looted {corpses_opened} corpses: "
                    f"{items_picked} items, {gold_picked} gold"
                ),
            )
        return ProcedureResult(
            success=False,
            reason=FailureReason.MISSING_RESOURCE,
            message=f"Opened {corpses_opened} corpses but found nothing useful",
        )


def _loot_valuable_graphics() -> set[int]:
    """Graphics worth picking up from corpses during deadlock recovery."""
    from anima.procedures.craft_blacksmith import TONGS_GRAPHICS
    from anima.procedures.vendor_knowledge import ITEM_VENDOR_MAP
    from anima.skills.crafting.smelt import INGOT_GRAPHICS
    from anima.skills.crafting.tinker import TINKER_TOOLS_GRAPHICS
    from anima.skills.gathering.mine import ORE_GRAPHICS, PICKAXE_GRAPHICS

    SHOVEL_GRAPHICS = {0x0F39}
    return (
        ORE_GRAPHICS | INGOT_GRAPHICS | PICKAXE_GRAPHICS
        | SHOVEL_GRAPHICS | TINKER_TOOLS_GRAPHICS | TONGS_GRAPHICS
        | set(ITEM_VENDOR_MAP.keys())
    )


class _DesperateExploration:
    """Deadlock recovery Lv5 fallback: random long-distance walk.

    When all named locations, hunting, and relocation fail, the agent
    is trapped in a loop of exhausted destinations.  This procedure
    breaks the cycle by walking 30-50 tiles in a random compass
    direction, scanning for vendor NPCs, huntable monsters, and ground
    items at each stop.

    Unlike _WanderAndScavenge (±5-15 tiles near town), this covers
    fresh terrain the agent hasn't visited — potentially reaching
    vendors or spawns outside the known location set.
    """

    timeout_s = 900.0  # long multi-leg walk — exempt from default cap
    EXPLORE_STEPS = 8
    STEP_MIN = 30
    STEP_MAX = 50
    SCAN_RADIUS = 3  # tiles around each stop to check for ground items

    # 8 compass directions as (dx, dy) unit vectors
    _COMPASS = [
        (0, -1),   # N
        (1, -1),   # NE
        (1, 0),    # E
        (1, 1),    # SE
        (0, 1),    # S
        (-1, 1),   # SW
        (-1, 0),   # W
        (-1, -1),  # NW
    ]

    def __init__(self, ss) -> None:
        self.name = "desperate_exploration"
        self.description = "Random long-range exploration for opportunities"
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

        found_vendor = False
        found_vendor_mob = None
        found_monster = False
        found_monster_mob = None
        items_picked = 0
        spots_visited = 0

        # Shuffle compass directions so we don't always go the same way
        directions = list(self._COMPASS)
        random.shuffle(directions)

        for step_idx in range(self.EXPLORE_STEPS):
            ss = ctx.perception.self_state
            # Pick direction: cycle through shuffled compass
            dx, dy = directions[step_idx % len(directions)]
            step_dist = random.randint(self.STEP_MIN, self.STEP_MAX)
            target_x = ss.x + dx * step_dist
            target_y = ss.y + dy * step_dist

            logger.info(
                "desperate_explore_step",
                step=step_idx + 1,
                direction=f"({dx},{dy})",
                target=f"({target_x},{target_y})",
                dist=step_dist,
            )

            await go_to(ctx, target_x, target_y)
            spots_visited += 1

            ss = ctx.perception.self_state

            # --- Scan for vendor NPCs ---
            from anima.skills.trade.vendor import HUMAN_BODIES, _is_vendor
            for m in ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=18):
                if m.serial == ss.serial:
                    continue
                if m.body not in HUMAN_BODIES:
                    continue
                if _is_vendor(m):
                    found_vendor = True
                    found_vendor_mob = m
                    logger.info(
                        "desperate_explore_found_vendor",
                        name=m.name,
                        pos=f"({m.x},{m.y})",
                    )
                    break

            # --- Scan for huntable monsters ---
            # Include small town animals (cats, rats) — they may drop
            # gold and are often the only huntable creatures in town.
            # Respect no_gold_bodies (body types that never dropped gold)
            # and unreachable blacklist (serials we failed to reach).
            from anima.perception.enums import NotorietyFlag
            ATTACKABLE = {
                NotorietyFlag.ATTACKABLE,
                NotorietyFlag.CRIMINAL,
                NotorietyFlag.ENEMY,
                NotorietyFlag.MURDERER,
            }
            HUMAN_BODY_IDS = {0x0190, 0x0191}
            _no_gold = ctx.blackboard.get("_hunt_no_gold_bodies", set())
            _unreachable = ctx.blackboard.get("_hunt_unreachable", {})
            import time as _t
            _now = _t.time()
            for m in ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=18):
                if m.serial == ss.serial:
                    continue
                if m.notoriety not in ATTACKABLE:
                    continue
                if m.body in HUMAN_BODY_IDS and m.notoriety == NotorietyFlag.ATTACKABLE:
                    continue
                if m.body in _no_gold:
                    continue
                if m.serial in _unreachable and _now - _unreachable[m.serial] <= 300:
                    continue
                found_monster = True
                found_monster_mob = m
                logger.info(
                    "desperate_explore_found_monster",
                    name=m.name,
                    body=f"0x{m.body:04X}",
                    pos=f"({m.x},{m.y})",
                )
                break

            # --- Loot nearby corpses ---
            CORPSE_GRAPHIC = 0x2006
            looted_set: set[int] = ctx.blackboard.setdefault(
                "_looted_corpses", set(),
            )
            for it in list(ctx.perception.world.items.values()):
                if (it.container == 0
                        and it.graphic == CORPSE_GRAPHIC
                        and it.serial not in looted_set
                        and max(abs(it.x - ss.x), abs(it.y - ss.y)) <= 3):
                    cdist = max(abs(it.x - ss.x), abs(it.y - ss.y))
                    if cdist > 2:
                        arrived = await go_to(ctx, it.x, it.y)
                        if not arrived:
                            continue
                    from anima.client.packets import build_double_click
                    await ctx.conn.send_packet(build_double_click(it.serial))
                    await asyncio.sleep(1.5)
                    looted_set.add(it.serial)
                    # Pick up gold from inside the corpse
                    for ci in list(ctx.perception.world.items.values()):
                        if ci.container != it.serial:
                            continue
                        if ci.graphic not in (
                            0x0EED, *_loot_valuable_graphics(),
                        ):
                            continue
                        await ctx.conn.send_packet(
                            build_pick_up(ci.serial, ci.amount),
                        )
                        await asyncio.sleep(0.3)
                        await ctx.conn.send_packet(
                            build_drop_item(ci.serial, container=backpack),
                        )
                        await asyncio.sleep(0.3)
                        items_picked += 1
                        logger.info(
                            "desperate_explore_looted_corpse",
                            serial=f"0x{ci.serial:08X}",
                            graphic=f"0x{ci.graphic:04X}",
                        )

            # --- Pick up ground items ---
            items = _WanderAndScavenge._scan_valuables(ctx, ss)
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
                items_picked += 1
                logger.info(
                    "desperate_explore_picked",
                    serial=f"0x{item.serial:08X}",
                    graphic=f"0x{item.graphic:04X}",
                )

            # Stop early if we found something actionable
            if found_vendor or found_monster or items_picked >= 3:
                break

        # --- Approach discoveries ---
        # Exploration scans at 18 tiles but vendor interaction needs 8
        # and combat needs adjacency.  Walk toward the discovery so the
        # next planner tick can dispatch sell/hunt procedures.
        if found_vendor_mob:
            ss = ctx.perception.self_state
            vdist = max(abs(found_vendor_mob.x - ss.x),
                        abs(found_vendor_mob.y - ss.y))
            if vdist > 6:
                logger.info(
                    "desperate_explore_approaching_vendor",
                    name=found_vendor_mob.name,
                    dist=vdist,
                )
                await go_to(ctx, found_vendor_mob.x, found_vendor_mob.y)
        elif found_monster_mob:
            ss = ctx.perception.self_state
            mdist = max(abs(found_monster_mob.x - ss.x),
                        abs(found_monster_mob.y - ss.y))
            if mdist > 2:
                logger.info(
                    "desperate_explore_approaching_monster",
                    name=found_monster_mob.name,
                    dist=mdist,
                )
                await go_to(ctx, found_monster_mob.x, found_monster_mob.y)

        parts = []
        if found_vendor:
            parts.append("vendor spotted")
        if found_monster:
            parts.append("monster spotted")
        if items_picked:
            parts.append(f"{items_picked} items picked up")

        if parts:
            return ProcedureResult(
                success=True,
                message=(
                    f"Explored {spots_visited} spots: "
                    + ", ".join(parts)
                ),
            )

        return ProcedureResult(
            success=False,
            reason=FailureReason.MISSING_RESOURCE,
            message=f"Explored {spots_visited} spots in new directions, found nothing",
        )


class _SeekResurrection:
    """Priority 0 recovery: walk to a healer NPC and get resurrected.

    When the agent dies (HP=0), it becomes a ghost and cannot perform any
    action. The only recovery path is to find a healer NPC who can
    resurrect the player.

    In ServUO, healer NPCs detect ghosts via OnMovement — the ghost must
    ENTER the healer's 4-tile detection range (crossing the boundary).
    Double-clicking NPCs while dead is rejected by the server. So we walk
    away from the healer and then walk back to trigger the detection.
    """

    timeout_s = 900.0  # cross-town ghost walk — exempt from default cap
    MAX_HEALER_SEARCH_DIST = 18  # tiles to scan for healer NPCs
    RESURRECT_WAIT_TIMEOUT = 10.0  # seconds to wait after approaching
    RESURRECT_POLL_INTERVAL = 2.0
    HEALER_DETECT_RANGE = 4  # ServUO OnMovement detection radius
    WALK_AWAY_DIST = 6  # tiles to walk away before re-approaching
    HUMAN_BODIES = {0x0190, 0x0191}  # male, female human body IDs
    HEALER_FAIL_COOLDOWN = 300.0  # 5 min cooldown before retrying a failed location

    def __init__(self) -> None:
        self.name = "seek_resurrection"
        self.description = "Walk to healer NPC for resurrection"

    async def can_start(self, ctx) -> bool:
        return True

    async def run(self, ctx) -> ProcedureResult:
        import asyncio

        from anima.action.movement import go_to
        from anima.client.packets import (
            build_gump_response,
            build_opl_request,
            build_single_click,
        )
        from anima.world_knowledge import ALL_LOCATIONS

        ss = ctx.perception.self_state

        # Already alive — nothing to do (race between selection and execution)
        if ss.is_alive:
            return ProcedureResult(success=True, message="Already alive")

        logger.info(
            "seek_resurrection_start",
            pos=f"({ss.x},{ss.y})",
            hp=f"{ss.hits}/{ss.hits_max}",
        )

        # Step 0: Accept any pending Resurrection gump BEFORE walking.
        # ServUO keeps the player Frozen until a resurrection gump is
        # answered (PlayerMobile.Move refuses with cliloc 500111 while
        # HasGump(ResurrectGump) && !Alive).  If we try to walk first,
        # every packet gets rejected and _interact_with_healer cannot
        # execute its walk-away/walk-back detection trigger.
        if await self._dismiss_pending_res_gumps(ctx):
            return self._success_result(
                "Resurrected via gump response", ctx
            )
        ss = ctx.perception.self_state

        # Step 1: Check if a healer NPC is already nearby
        healer = self._find_nearby_healer(ctx, ss)
        if healer:
            logger.info("seek_resurrection_healer_nearby", name=healer.name)
            result = await self._interact_with_healer(ctx, healer.serial)
            if result:
                return result

        # Step 2: Walk to nearest healer location (skip recently-failed ones)
        import time as _t

        healer_locations = [
            loc for loc in ALL_LOCATIONS
            if "healer" in loc.name.lower()
        ]
        if not healer_locations:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="No healer locations known",
            )

        # Filter out locations that failed recently
        failed_healers: dict[str, float] = ctx.blackboard.get(
            "_failed_healer_locations", {}
        )
        now = _t.time()
        available = [
            loc for loc in healer_locations
            if now - failed_healers.get(loc.name, 0) > self.HEALER_FAIL_COOLDOWN
        ]
        if not available:
            # All locations failed recently — pick the one with the oldest
            # failure so we don't deadlock on the filter itself
            healer_locations.sort(
                key=lambda loc: failed_healers.get(loc.name, 0)
            )
            available = [healer_locations[0]]
            logger.info(
                "seek_resurrection_all_failed_retrying_oldest",
                target=available[0].name,
            )

        # Sort available by distance
        available.sort(
            key=lambda loc: max(abs(loc.nav_x - ss.x), abs(loc.nav_y - ss.y))
        )
        target_loc = available[0]
        dist = max(abs(target_loc.nav_x - ss.x), abs(target_loc.nav_y - ss.y))

        logger.info(
            "seek_resurrection_walking",
            target=target_loc.name,
            dist=dist,
        )
        if ctx.bus:
            ctx.bus.publish("action.start", {
                "message": f"☠ Dead — walking to {target_loc.name} ({dist} tiles)",
                "importance": 3,
            })

        arrived = await go_to(ctx, target_loc.nav_x, target_loc.nav_y)
        if not arrived:
            # Even partial progress is useful — we're closer to the healer
            ss = ctx.perception.self_state
            if ss.is_alive:
                return ProcedureResult(
                    success=True, message="Resurrected during travel"
                )
            # Mark the unreachable location as failed so the next attempt
            # rotates to a different healer.  Otherwise long-distance
            # pathfinding (e.g. 1000+ tiles to Britain) keeps exhausting
            # the step budget and we pick the same target every time.
            failed_locs = ctx.blackboard.setdefault(
                "_failed_healer_locations", {}
            )
            failed_locs[target_loc.name] = _t.time()
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"Could not reach {target_loc.name}",
            )

        # Check if we got resurrected just by arriving (some servers auto-res)
        ss = ctx.perception.self_state
        if ss.is_alive:
            logger.info("seek_resurrection_auto_res")
            return ProcedureResult(
                success=True, message="Resurrected by proximity to healer"
            )

        # Step 3: Request names for nearby NPCs and find healer by name
        ss = ctx.perception.self_state
        nearby = ctx.perception.world.nearby_mobiles(
            ss.x, ss.y, distance=self.MAX_HEALER_SEARCH_DIST
        )
        human_npcs = [
            m for m in nearby
            if m.serial != ss.serial and m.body in self.HUMAN_BODIES
        ]
        logger.info(
            "seek_resurrection_nearby_scan",
            total_mobiles=len(nearby),
            human_npcs=len(human_npcs),
            bodies=[(hex(m.serial), m.body, m.name or "?") for m in nearby
                    if m.serial != ss.serial][:10],
        )

        for mob in nearby:
            if not mob.name and mob.serial != ss.serial:
                await ctx.conn.send_packet(build_opl_request(mob.serial))
                await ctx.conn.send_packet(build_single_click(mob.serial))
        await asyncio.sleep(1.0)

        healer = self._find_nearby_healer(ctx, ss)
        if healer:
            result = await self._interact_with_healer(ctx, healer.serial)
            if result:
                return result

        # Step 4: No named healer found — walk near each human-body NPC
        # to trigger ServUO's OnMovement resurrection detection.
        # (Double-clicking NPCs is rejected for ghosts.)
        ss = ctx.perception.self_state
        # Refresh human NPC list after name requests
        nearby = ctx.perception.world.nearby_mobiles(
            ss.x, ss.y, distance=self.MAX_HEALER_SEARCH_DIST
        )
        human_npcs = [
            m for m in nearby
            if m.serial != ss.serial and m.body in self.HUMAN_BODIES
        ]
        if not human_npcs:
            logger.warning(
                "seek_resurrection_no_human_npcs",
                nearby_count=len(nearby),
            )

        for mob in human_npcs:
            result = await self._interact_with_healer(ctx, mob.serial)
            if result:
                return result
            ss = ctx.perception.self_state
            if ss.is_alive:
                return ProcedureResult(
                    success=True,
                    message=f"Resurrected by NPC 0x{mob.serial:08X}",
                )

        # Step 5: Speech fallback — say "help" near NPCs.
        # Some server configurations respond to ghost speech.
        ss = ctx.perception.self_state
        if not ss.is_alive:
            try:
                from anima.client.packets import build_unicode_speech
                await ctx.conn.send_packet(build_unicode_speech("help"))
                logger.info("seek_resurrection_speech_help")
                await asyncio.sleep(3.0)
                ss = ctx.perception.self_state
                if ss.is_alive:
                    return self._success_result(
                        "Resurrected after saying 'help'", ctx,
                    )
            except Exception as e:
                logger.warning("seek_resurrection_speech_error", error=str(e))

        # Record this location as failed so next attempt tries a different one
        failed_locs = ctx.blackboard.setdefault("_failed_healer_locations", {})
        failed_locs[target_loc.name] = _t.time()
        logger.info(
            "seek_resurrection_location_failed",
            location=target_loc.name,
            failed_count=len(failed_locs),
            known_locations=len(healer_locations),
        )

        return ProcedureResult(
            success=False,
            reason=FailureReason.BLOCKED,
            message=f"Reached {target_loc.name} but no healer responded"
                    f" (scanned {len(human_npcs)} human NPCs,"
                    f" {len(nearby)} total mobiles)"
                    f" — will try different location next",
            next_suggestion="seek_resurrection",
        )

    async def _interact_with_healer(self, ctx, healer_serial: int) -> ProcedureResult | None:
        """Walk near healer NPC to trigger OnMovement resurrection detection.

        ServUO healers detect ghosts via OnMovement: the ghost must cross
        INTO the 4-tile detection range. If already within range, we walk
        away first (6 tiles) then walk back. Double-clicking NPCs while
        dead is rejected by the server.
        """
        import asyncio
        import time

        from anima.action.movement import go_to

        healer = ctx.perception.world.mobiles.get(healer_serial)
        if not healer:
            return None

        ss = ctx.perception.self_state
        dist = max(abs(healer.x - ss.x), abs(healer.y - ss.y))

        logger.info(
            "seek_resurrection_approach",
            serial=hex(healer_serial),
            healer_pos=f"({healer.x},{healer.y})",
            dist=dist,
        )

        # If already within detection range, walk away first to reset trigger
        if dist <= self.HEALER_DETECT_RANGE:
            # Pending res gump from an earlier pass would Freeze this walk
            if await self._dismiss_pending_res_gumps(ctx):
                return self._success_result("Resurrected while repositioning", ctx)
            dx = ss.x - healer.x
            dy = ss.y - healer.y
            if dx == 0 and dy == 0:
                dx = 1  # arbitrary direction
            mag = max(abs(dx), abs(dy))
            away_x = healer.x + dx * self.WALK_AWAY_DIST // mag
            away_y = healer.y + dy * self.WALK_AWAY_DIST // mag
            logger.info(
                "seek_resurrection_walk_away",
                away_target=f"({away_x},{away_y})",
            )
            await go_to(ctx, away_x, away_y)

            ss = ctx.perception.self_state
            if ss.is_alive:
                return self._success_result("Resurrected while repositioning", ctx)

        # Walk to within 2 tiles of the healer to cross the detection boundary.
        # Re-dismiss any gump that arrived during the walk_away so the approach
        # isn't blocked by "You are frozen and cannot move."
        if await self._dismiss_pending_res_gumps(ctx):
            return self._success_result("Resurrected while repositioning", ctx)
        logger.info(
            "seek_resurrection_walk_to_healer",
            healer_pos=f"({healer.x},{healer.y})",
        )
        await go_to(ctx, healer.x, healer.y)

        ss = ctx.perception.self_state
        if ss.is_alive:
            return self._success_result("Resurrected by healer NPC", ctx)

        # Poll for resurrection gump
        deadline = time.monotonic() + self.RESURRECT_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(self.RESURRECT_POLL_INTERVAL)
            ss = ctx.perception.self_state

            # Accept any pending resurrection gump. Delegate to the shared
            # helper, which gates on the res-gump signature (CONTINUE/OK label
            # or a "resurrect"/"ghost" text line) before sending a button.
            # The old inline loop fell back to button 1 for *every* open gump,
            # so an unrelated gump (e.g. a welcome/status window with a reply
            # button) would get a spurious button-1 click while the real res
            # gump went unanswered — leaving the ghost Frozen until timeout.
            if await self._dismiss_pending_res_gumps(ctx):
                return self._success_result("Resurrected by healer NPC", ctx)
            ss = ctx.perception.self_state

            if ss.is_alive:
                return self._success_result("Resurrected by healer NPC", ctx)

        return None  # timeout — caller should try other options

    async def _dismiss_pending_res_gumps(self, ctx) -> bool:
        """Answer any pending Resurrection gump so the ghost can move again.

        ServUO's PlayerMobile.Move refuses every walk packet with
        cliloc 500111 ("You are frozen and cannot move") while a
        ResurrectGump is open on a dead player.  Button-id 1 is the
        CONTINUE choice on both the standard and the priced
        ResurrectGump.  If text-matching fails (cliloc mis-resolved or
        unusual layout), fall back to button_id=1 for res-looking
        gumps so we don't deadlock behind a parseable-but-unmatched
        gump.

        Returns True if the response resulted in the player being
        alive (so the caller can short-circuit with a success result).
        """
        import asyncio

        from anima.client.packets import build_gump_response

        ss = ctx.perception.self_state
        for gump_id, gump in list(ss.gumps.items()):
            cont_btn = gump.find_button_near_text("CONTINUE")
            if cont_btn is None:
                cont_btn = gump.find_button_near_text("OK")
            button_id: int | None = cont_btn.button_id if cont_btn else None
            if button_id is None:
                # Fallback: look for res-gump signature in the text lines
                joined = " ".join(gump.text_lines).lower()
                looks_like_res = (
                    "resurrect" in joined or "ghost" in joined
                )
                reply_btns = gump.reply_buttons()
                if looks_like_res and reply_btns:
                    button_id = 1  # ServUO ResurrectGump CONTINUE
                else:
                    logger.warning(
                        "seek_resurrection_gump_skip",
                        gump_id=hex(gump_id),
                        button_ids=[b.button_id for b in gump.buttons],
                        text_lines=gump.text_lines[:8],
                    )
                    continue
            button_id = self._safe_button(gump, button_id)  # never disconnect
            logger.info(
                "seek_resurrection_gump_accept",
                gump_id=hex(gump_id),
                button_id=button_id,
            )
            await ctx.conn.send_packet(
                build_gump_response(gump.serial, gump_id, button_id)
            )
            ss.gumps.pop(gump_id, None)
            await asyncio.sleep(2.0)
            ss = ctx.perception.self_state
            if ss.is_alive:
                return True
        return False

    @staticmethod
    def _safe_button(gump, preferred: int) -> int:
        """Coerce a gump-response button to one ServUO will accept.

        ServUO's PacketHandlers.DisplayGumpResponse calls state.Dispose()
        (DISCONNECTS the client) on any response whose button is neither 0 nor
        a real GumpButton/GumpImageTileButton entry. The young-player death
        gump matched our resurrection heuristic but has no button 1, so sending
        1 killed the connection mid-death-recovery. Button 0 ('close') is always
        valid; fall back to it when the preferred button isn't in the gump.
        """
        if preferred == 0:
            return 0
        valid = {b.button_id for b in getattr(gump, "buttons", [])}
        return preferred if preferred in valid else 0

    @staticmethod
    def _success_result(message: str, ctx) -> ProcedureResult:
        logger.info("seek_resurrection_success", message=message)
        # Clear failed-location tracking so future deaths start fresh
        ctx.blackboard.pop("_failed_healer_locations", None)
        if ctx.bus:
            ctx.bus.publish("action.end", {
                "message": "✓ Resurrected!",
                "importance": 3,
            })
        return ProcedureResult(success=True, message=message)

    @staticmethod
    def _find_nearby_healer(ctx, ss):
        """Find a healer NPC within search distance."""
        HEALER_NAMES = {"healer", "wandering healer", "resurrection"}
        for mob in ctx.perception.world.nearby_mobiles(
            ss.x, ss.y, distance=_SeekResurrection.MAX_HEALER_SEARCH_DIST
        ):
            if mob.serial == ss.serial:
                continue
            if mob.name:
                name_lower = mob.name.lower().strip()
                if any(h in name_lower for h in HEALER_NAMES):
                    return mob
        return None


class _DeathEscalate:
    """Death-recovery deadlock break: ghost stuck, no healer reached.

    Runs when ``seek_resurrection`` has failed N times in a row.  The
    usual cause is that every named healer location is either
    unreachable (long-distance pathfinding exhausts its step budget) or
    reached but no live healer NPC responds — leaving the ghost pinned
    with no way to re-enter the normal procedure ladder.

    Recovery steps:
      1. Clear ``_failed_healer_locations`` so the next
         ``seek_resurrection`` starts with a fresh rotation.
      2. Post a forum help request via :class:`DeadlockResolver`
         (shouts in-game and posts to the tavern forum), which also
         blocks for ~5 minutes giving the supervisor or other players
         time to rescue the ghost.
      3. Force a minimum 60s pause even when forum escalation is on
         cooldown so the planner doesn't tight-loop through
         seek_resurrection → _DeathEscalate → seek_resurrection.
    """

    MIN_PAUSE_S = 60.0

    def __init__(self, deadlock) -> None:
        self.name = "death_escalate"
        self.description = (
            "Forum escalation for ghost that cannot reach any healer"
        )
        self._deadlock = deadlock

    async def can_start(self, ctx) -> bool:
        return True

    async def run(self, ctx) -> ProcedureResult:
        import asyncio
        import time as _t

        ss = ctx.perception.self_state
        logger.warning(
            "death_escalate_start",
            pos=f"({ss.x},{ss.y})",
            hp=f"{ss.hits}/{ss.hits_max}",
        )

        # Fresh rotation for the next seek_resurrection attempt.
        ctx.blackboard.pop("_failed_healer_locations", None)

        t0 = _t.time()
        try:
            await self._deadlock.escalate_to_forum(ctx)
        except Exception as e:
            logger.warning("death_escalate_forum_error", error=str(e))

        # Re-check alive state — someone may have resurrected us during
        # the forum wait.
        ss = ctx.perception.self_state
        if ss.is_alive:
            return ProcedureResult(
                success=True,
                message="Resurrected during forum help wait",
            )

        # Ensure we pause at least MIN_PAUSE_S to avoid tight-looping when
        # the 30-min forum cooldown makes escalate_to_forum return early.
        elapsed = _t.time() - t0
        remaining = self.MIN_PAUSE_S - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

        return ProcedureResult(
            success=True,
            message="Posted ghost help request; cleared healer rotation",
        )


# Survival floor for COMBAT death-recovery (backlog rank 6, apprentice track).
# Healing in place fails when surrounded (melee cancels the bandage), so a
# wounded-and-swarmed warrior must FLEE first; and a freshly-resurrected agent
# revives naked next to its corpse and must recover its gear before resuming —
# neither path existed (Priority 1 only healed in place; corpse looting was
# gated behind the miner-bootstrap deadlock ladder).

# Flee floor sits ABOVE both the in-place heal floor (0.30) and combat's
# RETREAT_HP_PCT (35%): a warrior that broke off a fight at 35% HP and is still
# surrounded must keep retreating instead of bandaging in place (where the
# adjacent swarm cancels the bandage). 0.40 covers the whole 30–40% band.
_FLEE_HP_PCT = 0.40        # below this HP fraction...
_FLEE_HOSTILE_COUNT = 3    # ...with at least this many hostiles, flee not heal
# After this many consecutive flee selections without breaking contact, stop
# fleeing and fall through to heal-in-place. A fully-surrounded agent can't path
# away (and _FleeFromHostiles reports success on the no-op move, so the
# starvation breaker never demotes it) — without this cap the flee gate loops
# forever and the agent never gets to bandage. 5 ticks ~= one full heal attempt.
_FLEE_MAX_CONSECUTIVE = 5
# Radius (Chebyshev tiles) of the swarm that drives BOTH the flee decision and
# the flee *direction*. The gate (``_should_flee_swarm``) counts hostiles within
# this radius; ``_FleeFromHostiles.run`` must compute its escape centroid over
# the SAME population. Scanning a wider radius for the centroid lets a distant
# hostile on the far side of the agent skew the away-vector — in the worst case
# flipping it so the agent flees straight INTO the close swarm that wounded it
# (e.g. 3 mobs hugging the agent + 1 mob 11 tiles behind: the far mob drags the
# centroid past the agent, and the "away" direction now points at the 3). Pin
# both to one radius so the swarm we run from is exactly the swarm we measured.
_FLEE_SCAN_DIST = 6
# Human body ids (male, female). A bare-gray (ATTACKABLE) human is NOT a foe the
# warrior ever swings at — combat_loop._find_target / _adjacent_hostiles both
# drop it — so the survival flee gate must drop it too (see _is_live_hostile).
_HUMAN_BODIES = {0x0190, 0x0191}
# Heal-in-place floor. Combat breaks off at RETREAT_HP_PCT (35%) and won't
# re-engage until hunt_nearby.can_start clears 40%; the Priority-1b flee gate
# only fires when SWARMED. Without matching this floor to the 40% re-engage
# floor, a non-swarmed agent at 30–40% HP would neither re-hunt, flee, nor heal
# (the old gate was a hardcoded 0.30) and fell through to the profession loop
# wounded. Pinned to _FLEE_HP_PCT so both survival branches share one floor.
_HEAL_IN_PLACE_HP_PCT = _FLEE_HP_PCT  # 0.40
_HOSTILE_NOTORIETY = None  # set lazily (avoid import at module load)


def _bandage_should_yield_to_combat(ctx, ss) -> bool:
    """True when a standalone bandage_self should defer to hunting.

    ``bandage_self.can_start`` returns True for any wound below 95% HP, and the
    profession loop is first-startable-wins — so for the adventurer (whose loop
    leads with ``bandage_self``) a single scratch makes the warrior stop and
    bandage instead of swinging, even at 94% HP with a mob adjacent. That is the
    documented COMBAT-uptime leak (engaged-vs-idle seed variance).

    Yield (return True, skip the standalone bandage) only when BOTH hold:
      * a combat target is within ENGAGE_RANGE — there is a fight to keep; and
      * HP is above combat's retreat floor (RETREAT_HP_PCT) — we are not in real
        danger, so the fight's own interleaved heal (``_maybe_bandage`` at 85%)
        will bandage us mid-swing without forfeiting the attack.

    When no target is in range we DON'T yield, so the agent still heals up
    between fights; when critically wounded the Priority-1 heal-in-place gate
    (< 40%) has already fired above, so reaching here that low is the safety
    case and we also DON'T yield.
    """
    from anima.procedures.combat_loop import RETREAT_HP_PCT, _find_target

    if ss.hits_max > 0 and ss.hp_percent <= RETREAT_HP_PCT:
        return False
    return _find_target(ctx) is not None


def _loot_flush_should_yield_to_combat(ctx) -> bool:
    """True when the adventurer loot-flush (sell_to_vendor) should defer to a
    live fight.

    The loot-flush gate preempts the entire first-startable profession loop —
    hunt_nearby included — the instant the sellable backlog crosses
    LOOT_SELL_BACKLOG_THRESHOLD. With a vendor in range (the survival-arena
    layout co-locates it with the spawn) that drags the warrior away from an
    engaged mob to sell, every loop, forfeiting swings and exposing its back to
    the swarm. Yield (return True) whenever combat has a target in ENGAGE_RANGE,
    so the loot only flushes BETWEEN fights. Unlike the bandage gate this has no
    HP carve-out: when critically wounded the Priority-1 survival branches have
    already returned above, so any path reaching the loot-flush is healthy
    enough that an in-range target means "keep fighting, sell later".
    """
    from anima.procedures.combat_loop import _find_target

    return _find_target(ctx) is not None


def _attackable_set():
    from anima.perception.enums import NotorietyFlag
    return {
        NotorietyFlag.ATTACKABLE,
        NotorietyFlag.CRIMINAL,
        NotorietyFlag.ENEMY,
        NotorietyFlag.MURDERER,
    }


def _is_live_hostile(m, ss, attackable) -> bool:
    """True only for a *living* enemy mobile that is not the agent itself.

    The friend/foe gate is notoriety, but a just-felled mob lingers in
    ``world.mobiles`` — with a known health bar reading zero, or already a
    ghost body (``MobileInfo.is_dead``) — until its 0x1D Delete arrives. The
    combat loop already drops corpses from its target/surround scans
    (``combat_loop._find_target`` / ``_adjacent_hostiles``); the survival flee
    gate must apply the same filter. Otherwise, right after clearing a swarm,
    the corpses still satisfy the notoriety test and inflate the hostile count,
    so a wounded agent flees from dead bodies instead of healing in place on a
    fight it has already won. ``is True`` (not just truthy) so SimpleNamespace /
    Mock test mobs that omit ``is_dead`` are treated as alive, never corpses.

    The notoriety gate must also match combat's: ``combat_loop._find_target``
    and ``_adjacent_hostiles`` both *exclude* a human body whose notoriety is
    only bare ATTACKABLE (a gray townsperson / passer-by the warrior never
    swings at — humans are engaged only when CRIMINAL/ENEMY/MURDERER). The flee
    gate previously counted such grays as the "swarm", so a wounded agent could
    be driven to flee (and have heal-in-place suppressed) by a crowd of harmless
    gray humans it would never fight — a friend/foe asymmetry between who we
    attack and who we run from. Mirror the combat filter here so the same
    population drives both decisions. Mobs (non-human bodies) are unaffected;
    test mobs that omit ``body`` default to 0 (non-human) and stay hostile.

    The same asymmetry exists for INVULNERABLE / blessed mobiles (yellow health
    bar): ``combat_loop._find_target`` and ``_adjacent_hostiles`` both *skip* a
    mobile whose ``is_yellow_health`` is set (a Healer / protected town NPC that
    takes zero damage however long we swing). The flee gate previously counted
    such an immortal mobile as part of the "swarm". Acutely relevant since the
    survival-arena Healer NPC was co-located at the resurrection spot (kernel
    commits K1+/K1/K3): once the agent clears the Ettins/HeadlessOnes, the
    adjacent immortal Healer keeps the hostile count above the swarm floor, so a
    wounded agent flees the unkillable Healer instead of healing in place on a
    fight it has already won — the exact win-then-bleed-out failure this gate
    exists to prevent. ``is True`` (not truthy) so test mobs lacking the field
    aren't mis-excluded.
    """
    from anima.perception.enums import NotorietyFlag

    if getattr(m, "serial", None) == getattr(ss, "serial", None):
        return False
    notoriety = getattr(m, "notoriety", None)
    if notoriety not in attackable:
        return False
    # An invulnerable / blessed mobile (yellow health bar) can never be killed
    # and is never a foe we fight, so it is never a foe we flee from either.
    if getattr(m, "is_yellow_health", False) is True:
        return False
    # A bare-gray human is not a foe we ever attack -> not one we flee from.
    if getattr(m, "body", 0) in _HUMAN_BODIES and notoriety == NotorietyFlag.ATTACKABLE:
        return False
    return getattr(m, "is_dead", False) is not True


def _count_hostiles(ctx, ss, dist: int) -> int:
    attackable = _attackable_set()
    return sum(
        1
        for m in ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=dist)
        if _is_live_hostile(m, ss, attackable)
    )


def _flee_destination(ss, hostiles, flee_dist: int) -> tuple[int, int]:
    """Pick a tile ``flee_dist`` tiles away from the hostile centroid.

    The survival floor only fires when the agent is wounded AND swarmed, which
    is exactly the geometry where the agent tends to sit at (or very near) the
    centroid of the pack. In that case ``ss - centroid`` is the zero vector and
    the old code (``norm = math.hypot(dx, dy) or 1.0``) produced an escape
    delta of (0, 0) — a "flee" straight back to the agent's own tile. The
    bandage then gets interrupted by the still-adjacent swarm and the agent
    dies, the precise combat-sustainability collapse this guard exists to
    prevent.

    When the away-direction is degenerate, fall back to a deterministic cardinal
    escape (north) so the agent always breaks contact instead of standing still.
    """
    import math

    cx = sum(m.x for m in hostiles) / len(hostiles)
    cy = sum(m.y for m in hostiles) / len(hostiles)
    dx, dy = ss.x - cx, ss.y - cy
    norm = math.hypot(dx, dy)
    if norm < 1e-6:
        # Agent is on the hostile centroid (fully surrounded): no away-vector
        # exists, so commit to a fixed direction rather than fleeing in place.
        dx, dy, norm = 0.0, -1.0, 1.0
    fx = int(ss.x + (dx / norm) * flee_dist)
    fy = int(ss.y + (dy / norm) * flee_dist)
    return fx, fy


class _FleeFromHostiles:
    """Retreat away from a swarm when too wounded to fight. Healing in place
    fails surrounded (melee interrupts the bandage), so break contact first."""

    timeout_s = 30.0
    FLEE_DIST = 12

    def __init__(self) -> None:
        self.name = "flee_from_hostiles"
        self.description = "Retreat from a swarm when too wounded to fight"

    async def can_start(self, ctx) -> bool:
        return ctx.perception.self_state.is_alive

    async def run(self, ctx) -> ProcedureResult:
        from anima.action.movement import go_to

        ss = ctx.perception.self_state
        attackable = _attackable_set()
        hostiles = [
            m for m in ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=_FLEE_SCAN_DIST)
            if _is_live_hostile(m, ss, attackable)
        ]
        if not hostiles:
            return ProcedureResult(success=True, message="No hostiles to flee from")

        # Move directly away from the hostile centroid.
        fx, fy = _flee_destination(ss, hostiles, self.FLEE_DIST)
        logger.warning(
            "fleeing_swarm", hostiles=len(hostiles),
            hp=f"{ss.hits}/{ss.hits_max}", to=f"({fx},{fy})",
        )
        await go_to(ctx, fx, fy, run=True)
        return ProcedureResult(
            success=True,
            message=f"Fled from {len(hostiles)} hostiles to ({fx},{fy})",
            next_suggestion="bandage_self",
        )


class _RecoverAfterDeath:
    """Post-resurrection: walk to the (own) corpse, loot the gear back, and
    re-equip a weapon before resuming the profession loop."""

    timeout_s = 120.0
    SEARCH_DIST = 14

    def __init__(self) -> None:
        self.name = "recover_after_death"
        self.description = "Recover own corpse and re-equip after resurrection"

    async def can_start(self, ctx) -> bool:
        ss = ctx.perception.self_state
        if not ss.is_alive:
            return False
        from anima.actions.loot import find_corpses
        has_corpse = bool(find_corpses(ctx, max_dist=self.SEARCH_DIST))
        weaponless = not (ss.equipment.get(1) or ss.equipment.get(2))
        return has_corpse or weaponless

    async def run(self, ctx) -> ProcedureResult:
        from anima.action.movement import go_to
        from anima.actions.equip import equip_weapon_from_pack
        from anima.actions.loot import find_corpses, loot_corpse
        from anima.procedures.combat_loop import (
            ONE_HANDED_WEAPON_GRAPHICS,
            TWO_HANDED_WEAPON_GRAPHICS,
        )

        ss = ctx.perception.self_state
        looted = 0
        # Loot the nearest corpse(s) — right after res the agent's own corpse
        # is the closest (find_corpses returns nearest-first).
        for corpse in find_corpses(ctx, max_dist=self.SEARCH_DIST)[:2]:
            if max(abs(corpse.x - ss.x), abs(corpse.y - ss.y)) > 2:
                await go_to(ctx, corpse.x, corpse.y, run=True)
            res = await loot_corpse(ctx, corpse.serial)
            looted += (res.data or {}).get("items", 0) if hasattr(res, "data") else 0

        equipped = False
        if not (ss.equipment.get(1) or ss.equipment.get(2)):
            # Re-arm from the recovered gear. CRITICAL: a two-handed weapon
            # (axe/polearm — among the most common monster drops a dead warrior
            # looted back) reads its layer from ServUO tiledata and MUST go on
            # the two-handed layer (2). Calling equip_weapon_from_pack with the
            # full weapon set on the default one-handed layer strands a looted
            # two-hander loose on the cursor and returns a *soft* success
            # (equip_item PickUps it, the server refuses the wear) — so the
            # agent believes it re-armed, stays disarmed, and re-dies: the exact
            # die->res->naked->die loop this whole recovery path exists to break
            # (same bug class as combat_loop commit 8f8ec37 / b33c9e4). Try a
            # one-hander first (keeps the off-hand free for a shield), then fall
            # back to a two-hander on the correct layer.
            eq = await equip_weapon_from_pack(ctx, ONE_HANDED_WEAPON_GRAPHICS)
            equipped = bool(getattr(eq, "success", False))
            if not equipped:
                eq = await equip_weapon_from_pack(
                    ctx, TWO_HANDED_WEAPON_GRAPHICS, two_handed=True
                )
                equipped = bool(getattr(eq, "success", False))

        # One bounded recovery pass — clear the post-death flag either way.
        ctx.blackboard["_was_dead"] = False
        return ProcedureResult(
            success=True,
            message=f"Post-res recovery: looted {looted} item(s), re-equipped={equipped}",
        )
