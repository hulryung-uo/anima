"""HuntNearby — a bounded melee combat loop (COMBAT profession).

Per-swing skill gains on ServUO (BaseWeapon.cs): every swing rolls
CheckSkill on the weapon skill + Tactics + Anatomy, kill or no kill —
so simply staying engaged grinds the COMBAT category.

Safety rails: only engages ATTACKABLE/hostile notoriety (humans only if
clearly hostile), retreats to the engagement anchor below the HP floor,
caps each engagement at 45s, and always drops war mode on exit.

Throughput add-ons: equips a shield (Parrying stream), interleaves
fire-and-forget self-bandages between swings (Healing stream + uptime),
and loots adjacent corpses after each kill (gold/h).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.equip import equip_shield_from_pack, equip_weapon_from_pack
from anima.actions.inventory import find_in_backpack
from anima.actions.loot import find_corpses, loot_corpse
from anima.actions.target import use_on_object
from anima.client.packets import (
    build_attack,
    build_double_click,
    build_status_request,
    build_war_mode,
)
from anima.perception.enums import NotorietyFlag
from anima.procedures.bandage_self import BANDAGE_GRAPHICS
from anima.procedures.base import FailureReason, Procedure, ProcedureResult

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

SKILL_SWORDS = 40
SKILL_TACTICS = 27

# Same policy as anima/skills/combat/melee.py
ATTACKABLE_NOTORIETY = {
    NotorietyFlag.ATTACKABLE,
    NotorietyFlag.CRIMINAL,
    NotorietyFlag.ENEMY,
    NotorietyFlag.MURDERER,
}
HUMAN_BODIES = {0x0190, 0x0191}

WEAPON_GRAPHICS = {
    # swords
    0x0F51, 0x0F52,  # dagger
    0x0F5E, 0x0F5F,  # broadsword
    0x13FF, 0x1400,  # katana
    0x13B6, 0x13B7,  # scimitar
    0x0F61, 0x0F62,  # longsword
    0x13B9, 0x13BA,  # viking sword
    0x1441, 0x1440,  # cutlass
    # fencing
    0x1401,          # kryss
    0x1402, 0x1403,  # short spear
    0x1404, 0x1405,  # war fork
    # mace fighting
    0x0F5C, 0x0F5D,  # mace
    0x13B3, 0x13B4,  # club
    0x143A, 0x143B,  # maul
    0x1406, 0x1407,  # war mace
}

ENGAGE_RANGE = 10        # tiles to scan for targets
ENGAGEMENT_CAP_S = 45.0  # per-target time box
RETREAT_HP_PCT = 35.0    # break off and retreat below this
# Flee-threshold hysteresis. HP%% is read off mobile-status packets and can dip
# for a single tick — a damage packet landing just before the bandage it
# triggered resolves, or a momentary status-bar reading — only to recover on the
# next read. Breaking off on one sub-floor sample abandons a winnable fight and,
# because can_start only needs 40%%, the agent can immediately re-hunt and flap
# anchor<->mob. So a *single* dip arms a strike; we only retreat once HP is below
# the floor for this many consecutive ticks. Any tick at/above the floor clears
# the strike count. (Same "don't trust a transient blip" reasoning as the
# apprentice hp=0 death-debounce, commit 44fdedc.)
RETREAT_CONFIRM_TICKS = 2
TICK_S = 1.0

BANDAGE_HP_PCT = 85.0    # interleave a self-bandage below this
BANDAGE_HP_HEAVY_DPS_PCT = 98.0  # under heavy DPS, start as soon as we're scratched
# Falling at/above this many HP-percent-per-second counts as "heavy DPS" — at a
# ~8s bandage resolve time the agent would shed ~64% before the heal lands, so we
# raise the trigger threshold and get a heal in flight while HP is still high
# (the alternative is breaking at RETREAT_HP_PCT with no heal started).
HEAVY_DPS_PCT_PER_S = 8.0
BANDAGE_REAPPLY_S = 8.5  # min spacing — re-applying restarts the timer
LOOT_RANGE = 2           # container-open range for corpses
CORPSE_SPAWN_WAIT_S = 1.0  # kill → corpse item appears in world state

# Emergency heal-potion quaff. A bandage and a potion run on two *independent*
# timers in UO — drinking a heal potion gives an instant HP bump while a
# bandage is still applying. The interleaved bandage (above) takes ~8s to
# resolve; against heavy DPS HP can cross the RETREAT_HP_PCT floor inside that
# window and the agent flees a winnable fight with a heal still in flight. A
# heal potion is the right tool for exactly that gap: instant top-up to buy the
# bandage time to land. So: a potion is *not* a substitute for the bandage
# (which we keep using for the Healing skill stream and sustained throughput) —
# it is an emergency rescue layered on top, fired only when HP is critically
# low (POTION_HP_PCT, just above the retreat floor so it gets a chance before
# we break off). Heal potions are drunk by a plain double-click — NO target
# cursor (unlike a bandage) — and carry their own server-side use-delay, so we
# gate on a dedicated cooldown key independent of _bandage_last_ts.
HEAL_POTION_GRAPHICS = {
    0x0F0C,  # greater heal potion
    0x0F0B,  # heal potion
}
POTION_HP_PCT = 45.0     # quaff once HP drops below this (above the 35% floor)
# UO potion use-delay is ~10s wall-clock; never spam-quaff inside it (the drink
# would be refused server-side and we'd waste the loop tick).
POTION_COOLDOWN_S = 10.0

# Anti-surround positioning. A melee mob can only swing the agent while it is
# adjacent (Chebyshev distance 1), so each extra adjacent hostile is another
# full damage stream every swing cycle — being boxed in on all sides is what
# turns a survivable fight into an af<0.2 death (g_00022/16 at ~8 mobs). When
# this many hostiles are already adjacent we step one tile away from their
# centre of mass: the pack has to re-close to melee range, so for a step or two
# fewer of them are landing hits (and the bandage started by _maybe_bandage has
# room to resolve). We don't flee — we reposition and keep the engagement.
SURROUND_COUNT = 3
REPOSITION_STEP = 2      # tiles to back off from the hostile centroid


def _should_retreat(ctx: AgentContext, hp_pct: float, hits_max: int) -> bool:
    """True only after HP stays below the retreat floor for consecutive ticks.

    Debounces a single transient sub-floor HP reading (see
    ``RETREAT_CONFIRM_TICKS``): a lone dip arms a strike but does not break the
    engagement; the strike count must reach ``RETREAT_CONFIRM_TICKS`` before we
    retreat. A tick at/above the floor (or unknown HP, ``hits_max <= 0``) resets
    the count, so a recovered blip never carries over into a later real drop.
    """
    if hits_max <= 0 or hp_pct >= RETREAT_HP_PCT:
        ctx.blackboard["_retreat_strikes"] = 0
        return False
    strikes = ctx.blackboard.get("_retreat_strikes", 0) + 1
    ctx.blackboard["_retreat_strikes"] = strikes
    return strikes >= RETREAT_CONFIRM_TICKS


def _reset_engagement_state(ctx: AgentContext) -> None:
    """Clear the per-tick survival-timing scratch state before a fresh hunt.

    ``_should_retreat`` and ``_bandage_trigger_pct`` accumulate state in the
    blackboard *across ticks* (the consecutive-sub-floor strike count, and the
    last HP%/timestamp used to estimate incoming DPS). That carry-over is
    correct *within* one engagement but must NOT survive into the next one: a
    hunt that breaks off at the retreat floor leaves ``_retreat_strikes`` parked
    at ``RETREAT_CONFIRM_TICKS``. The agent then heals up and re-enters
    ``hunt_nearby`` (``can_start`` only needs 40% HP) with the strike count still
    armed — so the very first transient sub-floor HP read of the *new* fight
    (e.g. a stale status bar before the post-heal 0x11 lands) instantly satisfies
    the retreat check, abandoning a winnable fight and flapping anchor<->mob.
    The stale ``_bandage_hp_*`` trajectory likewise mis-estimates DPS off a gap
    that spans the heal/walk-back. Resetting at engagement start makes the
    debounce start counting fresh, exactly as it does on the first-ever fight.
    """
    ctx.blackboard["_retreat_strikes"] = 0
    ctx.blackboard.pop("_bandage_hp_last", None)
    ctx.blackboard.pop("_bandage_hp_ts", None)


def _bandage_trigger_pct(ctx: AgentContext, hp_pct: float, now: float) -> float:
    """Effective HP%% below which we should interleave a self-bandage.

    Normally ``BANDAGE_HP_PCT`` (85). But a self-bandage takes ~8s to resolve,
    so against multiple hard-hitting mobs a heal started at 85%% lands long
    after HP has crossed the 35%% retreat floor — the agent breaks off (or
    dies) with no heal in flight. We track HP between ticks and, when it is
    falling steeply, raise the trigger toward ``BANDAGE_HP_HEAVY_DPS_PCT`` so a
    heal is already running by the time HP reaches dangerous levels. Only a
    *drop* counts; healing/standing still never inflates the threshold.
    """
    last_pct = ctx.blackboard.get("_bandage_hp_last")
    last_ts = ctx.blackboard.get("_bandage_hp_ts")
    ctx.blackboard["_bandage_hp_last"] = hp_pct
    ctx.blackboard["_bandage_hp_ts"] = now
    if last_pct is None or last_ts is None:
        return BANDAGE_HP_PCT
    dt = now - last_ts
    if dt <= 0.0:
        return BANDAGE_HP_PCT
    drop_per_s = (last_pct - hp_pct) / dt  # positive when HP is falling
    if drop_per_s >= HEAVY_DPS_PCT_PER_S:
        return BANDAGE_HP_HEAVY_DPS_PCT
    return BANDAGE_HP_PCT


async def _maybe_bandage(ctx: AgentContext) -> None:
    """Fire-and-forget self-bandage between swings.

    Double-clicks a bandage (0x0E21) and answers the target cursor
    (0x6C) with the agent's own serial, then returns immediately — no
    waiting for the "You finish applying" journal line, so war mode
    stays on and attack re-sends continue while the bandage timer runs.

    Poison override: in ServUO (Bandage.cs ``Heal``) a bandage applied to a
    *poisoned* target spends its resolve attempting a CURE, not an HP heal —
    and poison ticks keep draining HP regardless of how full the bar reads.
    The HP-percent trigger alone therefore lets a poisoned agent at, say, 80%%
    HP sit there ticking down to death without ever applying a bandage
    (``hp_percent >= trigger_pct`` skips it every tick). ``self_state`` already
    carries ``is_poisoned`` (synced from the 0x17 health-bar status packet), so
    we let an active poison force a bandage independent of the HP gate. The
    reapply cooldown still applies — a cure attempt occupies the same ~8s
    bandage timer as a heal, so re-arming inside it is wasted.
    """
    ss = ctx.perception.self_state
    now = time.monotonic()
    trigger_pct = _bandage_trigger_pct(ctx, ss.hp_percent, now)
    if ss.hits_max <= 0:
        return
    poisoned = bool(getattr(ss, "is_poisoned", False))
    # Poison forces a cure-bandage even at high HP; otherwise gate on the
    # (DPS-adaptive) HP trigger as before.
    if not poisoned and ss.hp_percent >= trigger_pct:
        return
    if now - ctx.blackboard.get("_bandage_last_ts", 0.0) < BANDAGE_REAPPLY_S:
        return
    bandages = find_in_backpack(ctx, BANDAGE_GRAPHICS)
    if not bandages:
        return
    used = await use_on_object(ctx, bandages[0].serial, ss.serial, timeout=2.0)
    if used.success:
        ctx.blackboard["_bandage_last_ts"] = now
        logger.info(
            "combat_bandage",
            hp_pct=round(ss.hp_percent, 1),
            trigger_pct=round(trigger_pct, 1),
            poisoned=poisoned,
        )
    else:
        logger.debug("combat_bandage_failed", message=used.message)


async def _maybe_quaff_heal_potion(ctx: AgentContext) -> bool:
    """Drink a heal potion for an instant HP top-up when critically low.

    Returns True if a potion was quaffed this tick. Independent of the bandage
    timer (the two heal on separate server cooldowns), so this can fire while a
    bandage is mid-apply — that overlap is the whole point. Gated on three
    things, all of which must hold:

      1. HP is *known* and below POTION_HP_PCT (critical, near the retreat
         floor) — no point burning a potion on a flesh wound.
      2. The potion's own ~10s cooldown (POTION_COOLDOWN_S) has elapsed — UO
         refuses a drink inside the use-delay, so quaffing again is wasted.
      3. A heal potion is actually in the backpack — availability gate; an
         empty pack must be a clean no-op, never a phantom heal.

    A heal potion is a plain double-click with no target cursor, so we send the
    use packet directly and return — no wait, war mode stays on.
    """
    ss = ctx.perception.self_state
    now = time.monotonic()
    if ss.hits_max <= 0 or ss.hp_percent >= POTION_HP_PCT:
        return False
    if now - ctx.blackboard.get("_potion_last_ts", 0.0) < POTION_COOLDOWN_S:
        return False
    potions = find_in_backpack(ctx, HEAL_POTION_GRAPHICS)
    if not potions:
        return False
    await ctx.conn.send_packet(build_double_click(potions[0].serial))
    ctx.blackboard["_potion_last_ts"] = now
    logger.info(
        "combat_heal_potion",
        hp_pct=round(ss.hp_percent, 1),
        serial=f"0x{potions[0].serial:08X}",
    )
    return True


def _adjacent_hostiles(ctx: AgentContext) -> list:
    """Live attackable mobiles in melee range (Chebyshev distance <= 1).

    These are the mobs that can actually swing the agent this tick. Reuses the
    same notoriety / dead-body filtering as ``_find_target`` so a just-felled
    corpse lingering in world.mobiles never inflates the surround count.
    """
    ss = ctx.perception.self_state
    out = []
    for m in ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=1):
        if getattr(m, "serial", None) == ss.serial:
            continue
        if m.notoriety not in ATTACKABLE_NOTORIETY:
            continue
        if getattr(m, "is_dead", False) is True:
            continue
        if m.body in HUMAN_BODIES and m.notoriety == NotorietyFlag.ATTACKABLE:
            continue
        if max(abs(m.x - ss.x), abs(m.y - ss.y)) <= 1:
            out.append(m)
    return out


def _reposition_dest(ss, hostiles: list) -> tuple[int, int] | None:
    """Tile ``REPOSITION_STEP`` away from the hostile centroid, or None.

    Returns None when the hostiles are perfectly balanced around the agent
    (centroid == agent tile) — there is no "away" direction to take, so we
    stand and keep swinging rather than pick an arbitrary tile.
    """
    if not hostiles:
        return None
    cx = sum(m.x for m in hostiles) / len(hostiles)
    cy = sum(m.y for m in hostiles) / len(hostiles)
    # Unit step directly away from the pack's centre of mass.
    ax, ay = ss.x - cx, ss.y - cy
    sx = (ax > 0) - (ax < 0)
    sy = (ay > 0) - (ay < 0)
    if sx == 0 and sy == 0:
        return None
    return ss.x + sx * REPOSITION_STEP, ss.y + sy * REPOSITION_STEP


async def _reposition_if_surrounded(ctx: AgentContext) -> bool:
    """Step out of the pile when boxed in. Returns True if a step was taken.

    Only fires at/above ``SURROUND_COUNT`` adjacent hostiles. Best-effort: a
    blocked or no-op move never fails the hunt — we just keep swinging.
    """
    from anima.action.movement import go_to

    ss = ctx.perception.self_state
    hostiles = _adjacent_hostiles(ctx)
    if len(hostiles) < SURROUND_COUNT:
        return False
    dest = _reposition_dest(ss, hostiles)
    if dest is None:
        return False
    logger.info(
        "combat_reposition",
        adjacent=len(hostiles),
        pos=f"({ss.x},{ss.y})",
        dest=f"({dest[0]},{dest[1]})",
    )
    # Interrupt the move the moment we're no longer surrounded — one or two
    # tiles of separation is the whole point; don't walk out of the fight.
    await go_to(
        ctx, dest[0], dest[1], run=True,
        interrupt_check=lambda: len(_adjacent_hostiles(ctx)) < SURROUND_COUNT,
    )
    return True


async def _loot_fresh_corpses(ctx: AgentContext) -> int:
    """Loot corpses adjacent to the agent after a kill. Returns gold lifted."""
    await asyncio.sleep(CORPSE_SPAWN_WAIT_S)  # let the corpse spawn in
    looted: set[int] = ctx.blackboard.setdefault("_looted_corpses", set())
    gold = 0
    for corpse in find_corpses(ctx, max_dist=LOOT_RANGE):
        if corpse.serial in looted:
            continue
        looted.add(corpse.serial)
        result = await loot_corpse(ctx, corpse.serial)
        gold += result.data.get("gold", 0)
        logger.info(
            "hunt_loot",
            corpse=f"0x{corpse.serial:08X}",
            items=result.data.get("items", 0),
            gold=result.data.get("gold", 0),
            message=result.message,
        )
    return gold


# StatusRequest type 4 = basic stats (carries the mobile's hits/hits_max). A
# real client fires this when it puts up a mobile's health bar; ServUO does not
# stream a foe's hits/hits_max otherwise, so without it ``MobileInfo.hits``/
# ``hits_max`` stay at their 0 default for the whole fight. That silently breaks
# two things in this loop: the focus-fire sort key in ``_find_target`` (every
# un-queried mob looks full-HP at hp_frac 1.0) and the health-bar death check
# (``current.hits_max > 0 and current.hits <= 0`` can never fire). We request it
# once per serial — gated through the blackboard so it isn't re-sent every tick,
# mirroring ClassicUO's ``HitsRequest == Pending`` de-dup.
async def _request_target_status(ctx: AgentContext, serial: int) -> bool:
    """Send a 0x34 status request for ``serial`` once. Returns True if sent."""
    requested: set[int] = ctx.blackboard.setdefault("combat_status_requested", set())
    if serial in requested:
        return False
    requested.add(serial)
    await ctx.conn.send_packet(build_status_request(4, serial))
    logger.debug("hunt_status_request", target=f"0x{serial:08X}")
    return True


def _find_target(ctx: AgentContext):
    """Nearest attackable non-human mobile (humans only when hostile)."""
    ss = ctx.perception.self_state
    candidates = []
    for m in ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=ENGAGE_RANGE):
        if m.notoriety not in ATTACKABLE_NOTORIETY:
            continue
        # Skip mobiles that are already dead. A just-killed mob lingers in
        # world.mobiles (with a known health bar at zero, or a ghost body)
        # until its 0x1D Delete arrives. Without this guard, focus-fire — which
        # sorts the lowest-HP mob first — re-selects the corpse we just felled
        # (hp_frac==0 sorts to the very top) instead of finding a live target.
        # `is True` (not just truthy) so MagicMock/SimpleNamespace test mobs that
        # don't define is_dead aren't spuriously treated as corpses — only a real
        # MobileInfo.is_dead bool counts.
        if getattr(m, "is_dead", False) is True:
            continue
        if m.body in HUMAN_BODIES and m.notoriety == NotorietyFlag.ATTACKABLE:
            continue
        candidates.append(m)
    if not candidates:
        return None

    def _key(m):
        # Chebyshev distance (max of the axis deltas) is UO's real step cost —
        # a diagonal move is one step, so an enemy at (4,4) is 4 tiles away, not
        # 8. The whole codebase ranges on Chebyshev (world.nearby_mobiles' box
        # filter, craft_blacksmith._find_nearest, world_knowledge.nearest_
        # locations); using Manhattan here over-penalised diagonal targets and
        # could tiebreak onto a strictly farther-to-walk mob.
        dist = max(abs(m.x - ss.x), abs(m.y - ss.y))
        # Focus-fire: known-wounded mobs sort first (lower hits fraction =
        # higher priority), distance is the tiebreaker. Health is "known"
        # only when hits_max is a positive number — un-queried mobs default
        # hits/hits_max to 0, and test mobs may lack the fields entirely or
        # carry non-numeric mocks, so we coerce defensively and fall back to
        # hp_frac=1.0 (pure-nearest, today's behavior) whenever it's unknown.
        hits_max = getattr(m, "hits_max", 0)
        hits = getattr(m, "hits", 0)
        try:
            hp_frac = (hits / hits_max) if hits_max > 0 else 1.0
        except TypeError:
            hp_frac = 1.0
        return (hp_frac, dist)

    candidates.sort(key=_key)
    return candidates[0]


class HuntNearby(Procedure):
    timeout_s = 180.0
    name = "hunt_nearby"
    description = "Fight nearby hostile creatures with an equipped melee weapon."

    async def can_start(self, ctx: AgentContext) -> bool:
        ss = ctx.perception.self_state
        if not ss.is_alive:
            return False
        if ss.hits_max > 0 and ss.hp_percent < 40:
            return False  # too wounded to start a fight
        # Weapon in hand or in pack
        from anima.actions.inventory import find_in_backpack
        has_weapon = (
            bool(ss.equipment.get(1)) or bool(ss.equipment.get(2))
            or bool(find_in_backpack(ctx, WEAPON_GRAPHICS))
        )
        return has_weapon and _find_target(ctx) is not None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        from anima.action.movement import go_to

        ss = ctx.perception.self_state
        anchor = (ss.x, ss.y)
        ctx.blackboard["combat_anchor"] = anchor
        _reset_engagement_state(ctx)

        sw = ss.skills.get(SKILL_SWORDS)
        tc = ss.skills.get(SKILL_TACTICS)
        before = (sw.value if sw else 0.0) + (tc.value if tc else 0.0)

        if not ss.equipment.get(1) and not ss.equipment.get(2):
            equipped = await equip_weapon_from_pack(ctx, WEAPON_GRAPHICS)
            if not equipped.success:
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.MISSING_RESOURCE,
                    message="No weapon to fight with",
                )

        # Parrying stream: raise a shield if the left hand is free.
        # Best-effort — a missing shield never fails the hunt.
        if not ss.equipment.get(2):
            try:
                shield = await equip_shield_from_pack(ctx)
                if shield.success and shield.data:
                    logger.info(
                        "hunt_shield_equipped",
                        serial=f"0x{shield.data.get('serial', 0):08X}",
                    )
            except Exception as exc:
                logger.debug("hunt_shield_skip", error=str(exc))

        target = _find_target(ctx)
        if target is None:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="No hostile target nearby",
            )

        kills = 0
        gold_looted = 0
        retreated = False
        try:
            await ctx.conn.send_packet(build_war_mode(True))
            await _request_target_status(ctx, target.serial)
            await ctx.conn.send_packet(build_attack(target.serial))
            deadline = time.monotonic() + ENGAGEMENT_CAP_S

            while time.monotonic() < deadline and ctx.conn.connected:
                await asyncio.sleep(TICK_S)

                # Emergency instant top-up: a heal potion runs on its own timer
                # so it can rescue HP in the ~8s a bandage takes to resolve.
                # Fire it *before* the retreat check so an available potion gets
                # a chance to pull HP back above the floor and save the fight.
                await _maybe_quaff_heal_potion(ctx)

                if _should_retreat(ctx, ss.hp_percent, ss.hits_max):
                    retreated = True
                    break

                # Interleaved self-heal — sends bandage + self-target and
                # returns; the heal resolves while we keep swinging.
                await _maybe_bandage(ctx)

                # Anti-surround: if the pack has boxed us in, step out so fewer
                # of them are in melee range. Re-attack after, since stepping
                # away can interrupt the current swing target.
                if await _reposition_if_surrounded(ctx):
                    await ctx.conn.send_packet(build_attack(target.serial))

                current = ctx.perception.world.mobiles.get(target.serial)
                # Dead = removed from the world, or a KNOWN health bar at
                # zero. MobileInfo defaults hits/hits_max to 0 for mobiles
                # we never queried — treating that as dead made this loop
                # re-target every tick and never land a swing.
                target_dead = current is None or (
                    current.hits_max > 0 and current.hits <= 0
                )
                if target_dead:
                    kills += 1
                    gold_looted += await _loot_fresh_corpses(ctx)
                    target = _find_target(ctx)
                    if target is None:
                        break
                    await _request_target_status(ctx, target.serial)
                    await ctx.conn.send_packet(build_attack(target.serial))
                    deadline = time.monotonic() + ENGAGEMENT_CAP_S
                    continue

                # Close the gap if the target moved away. Run, don't walk: a
                # chased mob is almost always inside ENGAGE_RANGE (< 8 tiles), so
                # go_to's auto-run policy (run=None) walks the whole leg at
                # 400ms/step — the same/faster cadence a kiting mob moves at, so
                # the agent never re-closes melee range and the swing never
                # re-lands. Forcing run=True (200ms/step, like the retreat,
                # reposition and wander legs already do) runs the target down and
                # restores swings-per-engagement. The retreat interrupt still
                # breaks the chase the instant HP crosses the floor.
                dist = max(abs(current.x - ss.x), abs(current.y - ss.y))
                if dist > 1:
                    await go_to(
                        ctx, current.x, current.y, run=True,
                        interrupt_check=lambda: (
                            ss.hits_max > 0 and ss.hp_percent < RETREAT_HP_PCT
                        ),
                    )
                    await ctx.conn.send_packet(build_attack(target.serial))
        finally:
            # Never leave war mode on — it blocks vendors/meditation/etc.
            try:
                await ctx.conn.send_packet(build_war_mode(False))
            except Exception:
                pass

        if retreated:
            await go_to(ctx, *anchor, run=True)

        sw = ss.skills.get(SKILL_SWORDS)
        tc = ss.skills.get(SKILL_TACTICS)
        gained = max(0.0, (sw.value if sw else 0.0) + (tc.value if tc else 0.0) - before)

        return ProcedureResult(
            success=not retreated or kills > 0,
            reason=FailureReason.INTERRUPTED if retreated and kills == 0 else None,
            message=(
                f"Combat: {kills} kills, +{gained:.1f} weapon/tactics"
                + (f", looted {gold_looted} gold" if gold_looted else "")
                + (" (retreated low HP)" if retreated else "")
            ),
            skill_gains={SKILL_SWORDS: gained} if gained else {},
            next_suggestion="bandage_self" if retreated else "hunt_nearby",
        )


# Roam outward from the combat anchor in a rotating direction so a weaponed
# warrior with no target in ENGAGE_RANGE keeps hunting for one instead of
# falling through to the mining/smelt chain (the single biggest COMBAT-uptime
# leak — the "wander_for_combat" / "widen engage" content of elite hypotheses
# g_00091 / g_00070 that was named but never landed in the loop).
WANDER_RADIUS = 8
WANDER_DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
# After this many consecutive empty roams (area swept, no hostile), give up and
# yield to productive work instead of looping until the planner health-break.
WANDER_MAX_EMPTY = 4


class WanderForCombat(Procedure):
    timeout_s = 60.0
    name = "wander_for_combat"
    description = (
        "Roam near the combat anchor to find hostiles when none are in range — "
        "keeps a warrior from idling into the mining loop between fights."
    )

    async def can_start(self, ctx: AgentContext) -> bool:
        ss = ctx.perception.self_state
        if not ss.is_alive:
            return False
        if ss.hits_max > 0 and ss.hp_percent < 40:
            return False  # too wounded — let survival/bandage run first
        has_weapon = (
            bool(ss.equipment.get(1)) or bool(ss.equipment.get(2))
            or bool(find_in_backpack(ctx, WEAPON_GRAPHICS))
        )
        # Activate exactly when hunt_nearby cannot: armed, but no target in range.
        return has_weapon and _find_target(ctx) is None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        from anima.action.movement import go_to

        ss = ctx.perception.self_state
        # Orbit the anchor (where mobs spawn / were last fought), not drift away.
        ax, ay = ctx.blackboard.get("combat_anchor", (ss.x, ss.y))
        idx = ctx.blackboard.get("_wander_dir_idx", 0) % len(WANDER_DIRS)
        ctx.blackboard["_wander_dir_idx"] = idx + 1
        dx, dy = WANDER_DIRS[idx]
        dest_x, dest_y = ax + dx * WANDER_RADIUS, ay + dy * WANDER_RADIUS

        found: dict[str, object] = {}

        def _spotted() -> bool:
            t = _find_target(ctx)
            if t is not None:
                found["t"] = t
                return True  # interrupt the walk — engage immediately
            return False

        await go_to(ctx, dest_x, dest_y, run=True, interrupt_check=_spotted)

        if found.get("t") is not None or _find_target(ctx) is not None:
            ctx.blackboard["_wander_empty"] = 0
            return ProcedureResult(
                success=True,
                message="Spotted a hostile while roaming → engaging",
                next_suggestion="hunt_nearby",
            )
        # No target. Keep roaming for a few rounds (a momentary gap between
        # fights / a target just out of range), but bound it: once we've swept
        # the area in several directions and found nothing, this spot is cleared
        # of hostiles — YIELD so the agent does productive work (mining/etc.)
        # instead of roaming until the planner's 60s health-break fires. Open
        # world keeps re-engaging (mobs respawn/roam); a depleted arena doesn't.
        empty = ctx.blackboard.get("_wander_empty", 0) + 1
        ctx.blackboard["_wander_empty"] = empty
        if empty >= WANDER_MAX_EMPTY:
            ctx.blackboard["_wander_empty"] = 0
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message=f"No hostiles after {empty} roams → yielding to other work",
            )
        return ProcedureResult(
            success=True,
            message=f"Roamed toward ({dest_x},{dest_y}); no hostile yet ({empty}/{WANDER_MAX_EMPTY})",
            next_suggestion="wander_for_combat",
        )
