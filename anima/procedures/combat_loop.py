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


def _initiates_combat(ctx: AgentContext) -> bool:
    """Whether the persona is willing to PROACTIVELY start a fight.

    Mirrors the legacy BT melee gate (anima/skills/combat/melee.py): a
    ``pacifist`` persona "never initiates combat", so the planner's proactive
    combat procedures (HuntNearby walking onto a hostile in range,
    WanderForCombat roaming to find one) must stay off for it. Without this
    gate the documented ``combat_disposition`` field is silently dropped in
    the active planner path — a pacifist blacksmith/bard/thief still hunts.

    Only ``pacifist`` is gated here: ``defensive``/``aggressive``/unset keep
    the existing proactive behaviour (the COMBAT lineage depends on it), and
    a defensive persona's retaliation is handled by the survival chain, not by
    refusing to ever start. Default (no persona / unset field) is permissive.
    """
    persona = getattr(ctx, "persona", None)
    return getattr(persona, "combat_disposition", "defensive") != "pacifist"


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
    # axes (Swordsmanship in ServUO — every axe rolls the Swords skill on a
    # swing, so they grind the same COMBAT stream as the swords above). These
    # are among the most common monster/corpse weapon drops, so an adventurer
    # who loots one but has no equipped weapon must be able to START a fight
    # with it. Without these graphics here, HuntNearby.can_start /
    # WanderForCombat.can_start both see has_weapon=False and the warrior falls
    # straight through to the mining/smelt chain (the single biggest COMBAT-
    # uptime leak this loop exists to plug). Graphics verified against ServUO
    # Scripts/Items/Weapons/Axes/*.cs base(0x....) constructors and cross-
    # checked with vendor_knowledge._AXES.
    0x0F43, 0x0F44,  # hatchet
    0x0F45, 0x0F46,  # executioner's axe
    0x0F47, 0x0F48,  # battle axe
    0x0F49, 0x0F4A,  # axe
    0x0F4B, 0x0F4C,  # double axe
    0x0F4D, 0x0F4E,  # bardiche (polearm)
    0x13AF, 0x13B0,  # war axe
    0x13FA, 0x13FB,  # large battle axe
    0x143E, 0x143F,  # halberd (polearm)
    0x1443, 0x1444,  # two-handed axe
}

# Two-handed melee weapons among WEAPON_GRAPHICS. On ServUO every axe (BaseAxe)
# and polearm (BasePoleArm) carries Layer.TwoHanded in its tiledata — the
# weapon's layer is read straight from the art (BaseWeapon.cs:5167
# `Layer = (Layer)ItemData.Quality;`). So when the server equips one it goes to
# the TWO-HANDED layer (2), occupying BOTH hands, and BaseWeapon.cs
# CheckConflictingLayer (line 1005) REFUSES any attempt to wear it on the
# one-handed layer ("You already have something in both hands.", 500214).
#
# `equip_weapon_from_pack` defaults to `two_handed=False`, i.e. layer 1. The
# axe family was added to WEAPON_GRAPHICS (commit b33c9e4, "looted axe arms
# combat") so a warrior holding only a looted axe reads as armed in can_start —
# but execute() then tried to wear that axe on layer 1, which the server
# rejects. equip_item still PickUps it first (lifting it loose onto the cursor)
# and reports a soft unverified success, so the agent believes it is armed,
# stays disarmed, lands zero swings, and the whole COMBAT skill stream (Swords/
# Tactics/Anatomy/Parrying) never rolls. We must wear a two-hander on layer 2.
TWO_HANDED_WEAPON_GRAPHICS = {
    0x0F43, 0x0F44,  # hatchet
    0x0F45, 0x0F46,  # executioner's axe
    0x0F47, 0x0F48,  # battle axe
    0x0F49, 0x0F4A,  # axe
    0x0F4B, 0x0F4C,  # double axe
    0x0F4D, 0x0F4E,  # bardiche (polearm)
    0x13AF, 0x13B0,  # war axe
    0x13FA, 0x13FB,  # large battle axe
    0x143E, 0x143F,  # halberd (polearm)
    0x1443, 0x1444,  # two-handed axe
}
ONE_HANDED_WEAPON_GRAPHICS = WEAPON_GRAPHICS - TWO_HANDED_WEAPON_GRAPHICS

ENGAGE_RANGE = 10        # tiles to scan for targets
ENGAGEMENT_CAP_S = 45.0  # per-target time box
# Attack-request resend cadence. The combat loop sends the 0x05 attack request
# once per target — on engage, and again on a re-pick / focus-fire switch — and
# then relies on the server's ``Mobile.Combatant`` staying set to keep the swing
# timer running. But a target adjacent the whole fight hits NONE of the re-send
# paths (the switch and chase branches only fire at dist > 1), so for a target
# that stays in melee range the attack request is sent exactly ONCE for up to
# the full ENGAGEMENT_CAP_S. If that single request is dropped or raced — sent in
# the same tick as build_war_mode(True) before war mode has registered, or the
# combatant cleared by a momentary LOS/range blip mid-fight — nothing ever
# re-arms it: the agent stands in war mode beside a live mob landing ZERO swings
# until the 45s cap, and the whole COMBAT skill stream (Swords/Tactics/Anatomy/
# Parrying) never rolls. A real client periodically re-issues the attack request
# as a keepalive; ServUO treats a repeat 0x05 for the current combatant as a
# cheap no-op and a fresh one as a re-acquire, so re-sending is always safe. We
# re-send for a live adjacent target once this many seconds have elapsed since
# the last attack packet (engage / re-pick / switch / chase / reposition all
# refresh the timer, so the keepalive only fires when nothing else already
# re-attacked). Sized a touch above a swing cycle so it never throttles a healthy
# fight, only rescues a stalled one.
ATTACK_RESEND_S = 5.0
RETREAT_HP_PCT = 35.0    # break off and retreat below this
# Chase leash. The combat anchor (set at engagement start) is the centre of the
# bounded arena this loop is meant to fight in — but the gap-closing chase below
# follows the target wherever it goes, bounded *only* by the HP retreat interrupt
# and the 45s cap. A fast/kiting mob that stays just out of melee yet inside
# ENGAGE_RANGE therefore drags the agent unboundedly far from the anchor: tile by
# tile the chase re-targets the mob's new position, the mob steps again, and the
# agent is walked clean out of the arena (past where the WANDER_RADIUS roam would
# ever have searched) chasing one kiter — exactly the "drift away from the anchor"
# the loop's docstring and WanderForCombat ("orbit the anchor, not drift away")
# promise it won't do. So we leash the chase: never chase a target whose tile is
# more than this many tiles (Chebyshev) from the anchor. Sized ENGAGE_RANGE +
# WANDER_RADIUS so a mob anywhere the agent could legitimately have engaged or
# roamed to is still chaseable, but a kiter that has left the arena is let go —
# the engagement breaks and the agent returns to the anchor to re-center.
# (Literal 18 == ENGAGE_RANGE(10) + WANDER_RADIUS(8); WANDER_RADIUS is defined
# further down so we can't reference it here at module load.)
MAX_CHASE_FROM_ANCHOR = 18
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
# Max times we re-open a corpse that yielded nothing before giving up on it.
# A loot can fail transiently — the 0x3C contents haven't streamed in yet, or
# the weight gate broke before lifting anything because the pack was full. We
# only retire a corpse for good once it has cost us this many empty opens, so a
# pack that frees up (a sell/bank between fights) still recovers stranded gold.
LOOT_MAX_ATTEMPTS = 3

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
    # Unknown HP (hits_max == 0, e.g. the agent's own 0x11/0x16 status has not
    # streamed in yet at engagement start) is NOT a real reading. ``hp_percent``
    # returns a 100.0 *placeholder* in that case (SelfState.hp_percent), so
    # feeding it to ``_bandage_trigger_pct`` seeds the cross-tick DPS trajectory
    # with a phantom 100%. The next tick, when real HP finally arrives (say 60%),
    # the estimator reads a ~40 pct/s "drop" that never happened and latches the
    # heavy-DPS trigger (98%) off noise — defeating the very baseline that
    # ``_reset_engagement_state`` pops the trajectory to establish cleanly. Bail
    # BEFORE touching the trajectory so an unknown-HP tick neither reads nor
    # writes the DPS baseline; the first *known* reading is the clean baseline.
    if ss.hits_max <= 0:
        return
    trigger_pct = _bandage_trigger_pct(ctx, ss.hp_percent, now)
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
    things (plus a poison veto), all of which must hold:

      1. HP is *known* and below POTION_HP_PCT (critical, near the retreat
         floor) — no point burning a potion on a flesh wound.
      2. The potion's own ~10s cooldown (POTION_COOLDOWN_S) has elapsed — UO
         refuses a drink inside the use-delay, so quaffing again is wasted.
      3. A heal potion is actually in the backpack — availability gate; an
         empty pack must be a clean no-op, never a phantom heal.
      4. The agent is NOT poisoned. ServUO ``BaseHealPotion.Drink`` refuses a
         heal potion outright while ``from.Poisoned`` ("You can not heal
         yourself in your current state.") — nothing is consumed and no HP is
         restored. Quaffing anyway would still arm our 10s cooldown, burning
         the emergency rescue gate for a no-op. The cure-bandage path handles
         poison instead, so the potion simply yields to it here.

    A heal potion is a plain double-click with no target cursor, so we send the
    use packet directly and return — no wait, war mode stays on.
    """
    ss = ctx.perception.self_state
    now = time.monotonic()
    if ss.hits_max <= 0 or ss.hp_percent >= POTION_HP_PCT:
        return False
    if bool(getattr(ss, "is_poisoned", False)):
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
        # An invulnerable / blessed mobile (yellow health bar) can never be
        # felled — see _find_target for why we exclude it everywhere. A boxed-in
        # healer would otherwise inflate the surround count and trigger a
        # reposition away from a target that isn't even a threat.
        if getattr(m, "is_yellow_health", False) is True:
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
    # Per-corpse failed-open counter. A corpse only enters ``looted`` (and is
    # skipped forever) on a SUCCESSFUL lift or after LOOT_MAX_ATTEMPTS empty
    # opens. Marking it looted up-front — before/regardless of the lift result —
    # permanently stranded gold whenever the open raced the 0x3C contents
    # stream or the weight gate broke on a full pack: the next kill's pass saw
    # the serial already in ``looted`` and never retried, even after weight
    # freed up. We now retry until it actually pays out or proves empty.
    attempts: dict[int, int] = ctx.blackboard.setdefault("_loot_attempts", {})
    gold = 0
    for corpse in find_corpses(ctx, max_dist=LOOT_RANGE):
        if corpse.serial in looted:
            continue
        result = await loot_corpse(ctx, corpse.serial)
        gold += result.data.get("gold", 0)
        # A weight-gated open is PARTIAL — valuables remain in the corpse
        # because the pack hit its headroom band. Retiring it would strand
        # that remainder forever; leave it eligible so the next post-kill
        # pass (after a sell/bank frees up weight) picks up where this one
        # stopped. Check this FIRST, ahead of ``result.success``: when the
        # pack is so full that even the gold (sorted first) can't be lifted,
        # ``loot_corpse`` lifts nothing and returns ``success=False`` while
        # still flagging ``weight_gated=True``. Routing that through the
        # empty-open branch below counted it toward LOOT_MAX_ATTEMPTS and
        # retired a corpse that is NOT empty — only the pack was full —
        # stranding its gold the moment weight later freed up. A weight-gated
        # open, lifted-something or not, must never count as an empty open.
        if result.data.get("weight_gated"):
            attempts.pop(corpse.serial, None)
        elif result.success:
            # Emptied to completion — retire so we don't wastefully re-open it.
            looted.add(corpse.serial)
            attempts.pop(corpse.serial, None)
        else:
            # Empty open: count it, and only retire the corpse once it has
            # cost us LOOT_MAX_ATTEMPTS opens (so a genuinely-empty corpse
            # isn't re-opened on every single subsequent kill forever).
            attempts[corpse.serial] = attempts.get(corpse.serial, 0) + 1
            if attempts[corpse.serial] >= LOOT_MAX_ATTEMPTS:
                looted.add(corpse.serial)
                attempts.pop(corpse.serial, None)
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


def _corpse_serials(ctx: AgentContext) -> set[int]:
    """Serials of every corpse (graphic 0x2006) on the ground within range now.

    Snapshotted when a target is first engaged so ``_confirm_kill`` can tell a
    body THIS fight dropped from one that was already lying there.
    """
    return {c.serial for c in find_corpses(ctx, max_dist=ENGAGE_RANGE)}


def _confirm_kill(
    ctx: AgentContext,
    last_hits: int,
    last_hits_max: int,
    known_corpses: set[int],
) -> bool:
    """True when a target that left ``world.mobiles`` actually DIED.

    A mobile is dropped from ``world.mobiles`` by ``World.remove`` on a 0x1D
    Delete (handlers.handle_delete) and by ``prune_stale_mobiles`` — but UO
    sends a 0x1D Delete whenever a mobile leaves the player's visual range, not
    only when it dies, and a stale-update prune likewise fires on a mob that has
    simply pathed away. So ``current is None`` does NOT imply a kill: a fast
    kiter that runs out of view, or a mob the leash let go, disappears exactly
    the same way a felled one does. Crediting every disappearance as a kill
    inflates the kill count that feeds the COMBAT throughput metric, fires a
    phantom ``_loot_fresh_corpses`` pass, and masks a kiter escape (which the
    chase leash exists to handle) as a win.

    A real melee kill leaves positive evidence, so we require one of:

      * the target's LAST-KNOWN health bar was a real reading at/below zero
        (``last_hits_max > 0 and last_hits <= 0``) — we watched it die; or
      * a *new* corpse (graphic 0x2006) is on the ground within ``ENGAGE_RANGE``
        — i.e. a corpse whose serial was NOT already present (``known_corpses``)
        when this target was engaged.

    The corpse arm must only count a body THIS fight dropped. Corpses linger in
    ``world.items`` until they are looted and ``World.remove``'d — and a loot
    can fail (full pack / weight gate broke before lifting, see
    ``_loot_fresh_corpses``), leaving an earlier kill's corpse on the ground for
    the rest of the engagement. With a bare ``find_corpses(...)`` truthiness
    check, that stale body falsely confirms EVERY subsequent target that merely
    kites out of view as a kill — re-inflating the exact COMBAT-throughput
    metric the kiter-not-a-kill guard exists to protect, and firing a phantom
    re-loot pass on the already-emptied corpse each time. Requiring a serial
    absent from the pre-engagement snapshot closes that hole.

    Otherwise the target merely left view: not a kill.
    """
    if last_hits_max > 0 and last_hits <= 0:
        return True
    return any(
        c.serial not in known_corpses
        for c in find_corpses(ctx, max_dist=ENGAGE_RANGE)
    )


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
        # Skip invulnerable / blessed mobiles (yellow health bar). ServUO marks
        # Healers, town NPCs and other protected mobiles with the YELLOW_BAR flag
        # (MobileInfo.is_yellow_health, synced from the 0x78/0x77/0x17 status
        # packets) — they take zero damage no matter how long we swing. This is
        # the DYNAMIC invulnerable signal, distinct from the static INVULNERABLE
        # notoriety (which is already excluded by ATTACKABLE_NOTORIETY): a Healer
        # can read as gray/ATTACKABLE notoriety yet still be unkillable. Without
        # this guard such a mob is a valid candidate, gets selected (focus-fire
        # even sorts it FIRST once its hits read non-full), and the engagement
        # loop locks onto it — it never dies and never leaves view, so the loop
        # only re-picks at the 45s ENGAGEMENT_CAP_S, burning the whole window
        # landing zero real kills. Acutely relevant since the survival-arena
        # Healer NPC was co-located at the resurrection spot (kernel commits K1+/
        # K1/K3): the agent would farm swings on the immortal healer instead of
        # the Ettins/HeadlessOnes it was spawned beside. `is True` (not truthy)
        # so SimpleNamespace test mobs lacking the field aren't mis-excluded.
        if getattr(m, "is_yellow_health", False) is True:
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
        if not _initiates_combat(ctx):
            return False  # pacifist persona never starts a fight
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

        # A two-handed weapon already worn on layer 2 (the TwoHanded layer) leaves
        # layer 1 EMPTY — an axe/polearm occupies only layer 2, never layer 1. So
        # an agent that re-equipped a looted two-hander between fights (post-res
        # re-equip, commit f57c7e3) reads as ``not ss.equipment.get(1)`` and would
        # otherwise enter the equip block below: the one-handed attempt is
        # correctly refused (equip_weapon_from_pack sees the worn two-hander on
        # layer 2 and bails, per ServUO BaseWeapon.CheckConflictingLayer 500214),
        # and the two-handed fallback is gated on ``not ss.equipment.get(2)`` —
        # which the worn two-hander makes false — so execute() falls straight to
        # "No weapon to fight with" and ABORTS the hunt while fully armed. Detect
        # the worn two-hander up front and treat it as already-equipped: skip the
        # equip block (so we don't bail) AND mark equipped_two_handed so the shield
        # path below never tries to add a shield on top of it (the server would
        # refuse the wear and strand the shield on the cursor).
        offhand_serial = ss.equipment.get(2)
        worn_offhand = None
        if offhand_serial:
            # world.items is a plain dict in production; tolerate a test stub
            # that lacks .get (treat as "unknown offhand item" → not a two-hander).
            items = getattr(ctx.perception.world, "items", None)
            worn_offhand = items.get(offhand_serial) if hasattr(items, "get") else None
        already_two_handed = (
            worn_offhand is not None
            and getattr(worn_offhand, "graphic", None) in TWO_HANDED_WEAPON_GRAPHICS
        )
        equipped_two_handed = already_two_handed
        # The WEAPON hand is layer 1; layer 2 is the off-hand (shield / the second
        # half of a two-hander). Gate the weapon equip on the WEAPON hand being
        # empty — NOT on both hands being empty. The old `not get(1) and not
        # get(2)` guard skipped the whole equip block whenever layer 2 was already
        # occupied — e.g. a shield left equipped from a previous engagement (the
        # Parrying stream raises one and nothing ever takes it off) while the
        # weapon was disarmed/looted/lost so layer 1 is empty. can_start still
        # passes (a weapon sits in the pack), but execute saw the off-hand full,
        # skipped equipping, and the agent fought BARE-HANDED holding only a
        # shield: every swing rolls Wrestling, never the measured COMBAT stream
        # (Swords/Tactics/Anatomy), the exact disarm leak the two-handed-layer fix
        # (commit 8f8ec37) and the axe-graphics fix (b33c9e4) exist to plug. So we
        # equip a one-handed weapon into the free layer-1 even when a shield holds
        # layer 2. The two-hander fallback still needs BOTH hands, so it only runs
        # when layer 2 is also free (and equip_weapon_from_pack(two_handed=True)
        # additionally refuses if layer 1 somehow re-filled).
        if not already_two_handed and not ss.equipment.get(1):
            # Prefer a one-handed weapon so the off-hand stays free for a shield
            # (Parrying stream). Only fall back to a two-hander when that's all
            # the pack holds AND the off-hand is free — and then wear it on the
            # TWO-handed layer, because ServUO reads an axe/polearm's layer from
            # its tiledata and refuses to wear it one-handed (it would strand on
            # the cursor, disarming us).
            equipped = await equip_weapon_from_pack(ctx, ONE_HANDED_WEAPON_GRAPHICS)
            if not equipped.success and not ss.equipment.get(2):
                equipped = await equip_weapon_from_pack(
                    ctx, TWO_HANDED_WEAPON_GRAPHICS, two_handed=True
                )
                equipped_two_handed = equipped.success
            if not equipped.success:
                return ProcedureResult(
                    success=False,
                    reason=FailureReason.MISSING_RESOURCE,
                    message="No weapon to fight with",
                )

        # Parrying stream: raise a shield if the left hand is free.
        # Best-effort — a missing shield never fails the hunt.
        # A two-handed weapon already fills the left hand (layer 2) — never try
        # to add a shield on top of it: the server would refuse the wear and
        # equip_shield_from_pack would strand the shield loose on the cursor.
        if not equipped_two_handed and not ss.equipment.get(2):
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
        died = False
        leashed = False
        # Last health-bar reading we saw for the current target, so that when it
        # vanishes from world.mobiles we can tell a real kill (we watched it hit
        # zero, or a corpse is on the ground) from a kiter that simply left view
        # (0x1D Delete / stale prune). Reset whenever the target changes.
        last_target_hits = 0
        last_target_hits_max = 0
        # Corpses already on the ground when THIS target was engaged. A kill is
        # confirmed off corpse evidence only for a body absent from this set, so
        # an earlier kill's un-looted (full-pack) corpse can't falsely credit a
        # later kiter that merely left view. Re-snapshotted on every re-pick.
        known_corpses = _corpse_serials(ctx)

        async def _send_attack(serial: int) -> None:
            await ctx.conn.send_packet(build_attack(serial))
            ctx.blackboard["_attack_last_ts"] = time.monotonic()
        try:
            await ctx.conn.send_packet(build_war_mode(True))
            await _request_target_status(ctx, target.serial)
            await _send_attack(target.serial)
            deadline = time.monotonic() + ENGAGEMENT_CAP_S

            while time.monotonic() < deadline and ctx.conn.connected:
                await asyncio.sleep(TICK_S)

                # Death short-circuit. ``can_start`` gates on ``is_alive`` but the
                # engagement loop did not — so if the agent is *killed* mid-fight
                # (hits hit 0 → ghost) nothing here noticed: the loop kept spinning
                # up to the full 45s ENGAGEMENT_CAP_S, firing attack/bandage/potion
                # packets as a corpse (all rejected server-side) while the planner's
                # Priority-0 death branch — which runs seek_resurrection — sat
                # blocked behind this still-executing procedure. The retreat check
                # below can't cover this: it is HP-*floor* logic debounced over
                # RETREAT_CONFIRM_TICKS, so a 0-HP death actually takes TWO ticks to
                # trip it, and even then routes to bandage_self (a ghost can't
                # bandage) instead of resurrection. Death is a terminal state, not a
                # low-HP blip: bail immediately so control returns to the planner.
                # ``hits_max > 0`` guards the un-queried/test default where HP is
                # simply unknown (is_alive already treats hits_max==0 as alive).
                if ss.hits_max > 0 and not ss.is_alive:
                    died = True
                    break

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
                repositioned = await _reposition_if_surrounded(ctx)
                if repositioned:
                    await _send_attack(target.serial)

                current = ctx.perception.world.mobiles.get(target.serial)
                # Dead = a KNOWN health bar at zero, or gone from the world with
                # positive death evidence. MobileInfo defaults hits/hits_max to 0
                # for mobiles we never queried — treating that as dead made this
                # loop re-target every tick and never land a swing.
                #
                # `current is None` (dropped from world.mobiles) is NOT proof of
                # death: a 0x1D Delete also fires when a mob leaves visual range
                # and prune_stale_mobiles reaps a mob that pathed away — so a
                # kiter that ran out of view disappears exactly like a felled one.
                # `_confirm_kill` gates the no-current case on real evidence (a
                # last-seen zero health bar, or a corpse on the ground); without
                # it a fled kiter is mis-credited as a kill (inflating the COMBAT
                # throughput metric) and triggers a phantom loot pass.
                if current is not None:
                    last_target_hits = current.hits
                    last_target_hits_max = current.hits_max
                    target_dead = current.hits_max > 0 and current.hits <= 0
                else:
                    target_dead = _confirm_kill(
                        ctx, last_target_hits, last_target_hits_max, known_corpses
                    )
                    if not target_dead:
                        # Target left view without dying — re-pick a closer live
                        # hostile (or break) but DON'T credit a kill or loot.
                        logger.info(
                            "combat_target_left_view",
                            target=f"0x{target.serial:08X}",
                        )
                if target_dead:
                    kills += 1
                    gold_looted += await _loot_fresh_corpses(ctx)
                # Re-pick a target when the current one is gone from the world
                # (dead OR left view) or confirmed dead by its health bar. A live
                # kiter that left view re-targets here too — without a kill credit
                # or a loot pass — so the next _find_target picks a closer foe.
                if current is None or target_dead:
                    target = _find_target(ctx)
                    if target is None:
                        break
                    last_target_hits = 0
                    last_target_hits_max = 0
                    # Re-baseline so the just-dropped corpse (and any unlooted
                    # earlier body) counts as pre-existing for the NEW target —
                    # only a corpse this next fight drops will confirm its kill.
                    known_corpses = _corpse_serials(ctx)
                    await _request_target_status(ctx, target.serial)
                    await _send_attack(target.serial)
                    deadline = time.monotonic() + ENGAGEMENT_CAP_S
                    continue

                # Attack-request keepalive. A target that stays in melee range the
                # whole fight reaches neither the focus-fire switch nor the chase
                # re-attack below (both gated on dist > 1), so the only 0x05 it
                # ever got was the engage send — if that was dropped/raced the
                # agent stands here in war mode landing no swings. Re-issue the
                # attack on a cadence so the combatant is re-armed within a few
                # seconds; any other re-attack this tick already refreshed the
                # timer, so this only fires when nothing else did.
                if time.monotonic() - ctx.blackboard.get("_attack_last_ts", 0.0) >= ATTACK_RESEND_S:
                    await _send_attack(target.serial)

                # Re-evaluate the focus-fire pick before committing to a chase.
                # The loop otherwise locks onto whatever target it first picked
                # and only re-runs _find_target when *that* mob dies — so a fast
                # kiter (or one that pathed away) drags the agent across the map
                # chasing it while a closer, reachable hostile stands adjacent
                # and swings freely. _find_target already encodes the right
                # priority (lowest HP fraction, nearest as tiebreak); we just
                # consult it the moment a chase would begin. If it names a
                # *different* live candidate strictly closer than the current
                # target, switch the swing to it rather than running the fleeing
                # one down. Strictly-closer only, so a mob merely tied on
                # distance never causes attack-packet thrash.
                cur_dist = max(abs(current.x - ss.x), abs(current.y - ss.y))
                if cur_dist > 1:
                    best = _find_target(ctx)
                    if best is not None and best.serial != target.serial:
                        best_dist = max(abs(best.x - ss.x), abs(best.y - ss.y))
                        if best_dist < cur_dist:
                            logger.info(
                                "combat_switch_target",
                                from_=f"0x{target.serial:08X}",
                                to=f"0x{best.serial:08X}",
                                from_dist=cur_dist,
                                to_dist=best_dist,
                            )
                            target = best
                            current = best
                            # Re-baseline the kill-confirmation tracking onto the
                            # NEW target, exactly as the dead/left-view re-pick
                            # path above does. The switch carried over the OLD
                            # target's last-seen health bar and the engage-start
                            # corpse snapshot — so if the freshly-switched target
                            # then kited out of view next tick, _confirm_kill
                            # would judge it against the previous target's reading
                            # AND count any corpse that dropped *after* the
                            # original engagement (a body this engagement already
                            # felled and left unlooted) as "new" evidence,
                            # crediting a phantom kill and firing a bogus loot pass
                            # — the same stale-corpse false-confirm the re-pick
                            # snapshot (commit 5195230) closes, missed here.
                            last_target_hits = 0
                            last_target_hits_max = 0
                            known_corpses = _corpse_serials(ctx)
                            await _request_target_status(ctx, target.serial)
                            await _send_attack(target.serial)

                # Close the gap if the target moved away. Run, don't walk: a
                # chased mob is almost always inside ENGAGE_RANGE (< 8 tiles), so
                # go_to's auto-run policy (run=None) walks the whole leg at
                # 400ms/step — the same/faster cadence a kiting mob moves at, so
                # the agent never re-closes melee range and the swing never
                # re-lands. Forcing run=True (200ms/step, like the retreat,
                # reposition and wander legs already do) runs the target down and
                # restores swings-per-engagement. The retreat interrupt still
                # breaks the chase the instant HP crosses the floor.
                #
                # ...but NOT on a tick we just repositioned. _reposition_if_
                # surrounded deliberately steps REPOSITION_STEP (2) tiles away
                # from the hostile centroid to thin the pack — which by
                # construction leaves the primary target at dist>1. Running the
                # gap-closing chase in the same iteration walks straight back
                # onto that target's tile, re-entering the pile and undoing the
                # reposition before a single swing-cycle of separation lands (the
                # whole point of the anti-surround step, and the mechanism behind
                # the af<0.2 surrounded-warrior deaths it exists to prevent). Let
                # the separation hold for this tick; the pack re-closes on its own.
                dist = max(abs(current.x - ss.x), abs(current.y - ss.y))
                if dist > 1 and not repositioned:
                    # Leash the chase to the arena: if the target's tile is
                    # beyond MAX_CHASE_FROM_ANCHOR from the anchor, it has kited
                    # out of bounds — don't follow it across the map. Break the
                    # engagement and let go_to(anchor) below re-center the agent
                    # (a closer reachable hostile, if any, is re-picked next time
                    # hunt_nearby runs). Without this the chase re-targets the
                    # fleeing mob's new tile every tick and drifts the agent
                    # unboundedly off the anchor.
                    from_anchor = max(
                        abs(current.x - anchor[0]), abs(current.y - anchor[1])
                    )
                    if from_anchor > MAX_CHASE_FROM_ANCHOR:
                        logger.info(
                            "combat_chase_leashed",
                            target=f"0x{target.serial:08X}",
                            from_anchor=from_anchor,
                            leash=MAX_CHASE_FROM_ANCHOR,
                        )
                        leashed = True
                        break
                    await go_to(
                        ctx, current.x, current.y, run=True,
                        interrupt_check=lambda: (
                            ss.hits_max > 0 and ss.hp_percent < RETREAT_HP_PCT
                        ),
                    )
                    await _send_attack(target.serial)
        finally:
            # Never leave war mode on — it blocks vendors/meditation/etc.
            try:
                await ctx.conn.send_packet(build_war_mode(False))
            except Exception:
                pass

        # Re-center on the anchor on ANY disengage that left us off it — not just
        # a retreat or a leash break. The engagement-cap timeout (deadline elapsed
        # while mid-chase) and a connection drop both exit the loop normally with
        # retreated/leashed still False, yet the gap-closing chase above can have
        # walked the agent up to MAX_CHASE_FROM_ANCHOR tiles off the anchor. Only
        # walking back on retreat/leash stranded the agent there after every cap
        # timeout, drifting the combat anchor out from under WanderForCombat's
        # orbit — exactly the "drift away from the anchor" the docstring and the
        # leash promise the loop won't do. So we gate the walk-back on actual
        # displacement: if we're still standing on the anchor (the common kill-
        # fest case that never chased anywhere) it's a no-op; otherwise we return.
        # A dead agent is the exception: a ghost can't meaningfully path back to
        # the arena anchor and the planner's Priority-0 death branch owns movement
        # now (it walks to a healer), so skip the walk-back entirely on death and
        # hand control straight back.
        if not died and (ss.x, ss.y) != anchor:
            await go_to(ctx, *anchor, run=True)

        sw = ss.skills.get(SKILL_SWORDS)
        tc = ss.skills.get(SKILL_TACTICS)
        gained = max(0.0, (sw.value if sw else 0.0) + (tc.value if tc else 0.0) - before)

        # A leash break is not a failure: the agent is healthy and has merely
        # let a kiter go and re-centred on the anchor, so it should keep hunting
        # (hunt_nearby), unlike a low-HP retreat which hands off to bandage_self.
        # A death is neither: control must fall through to the planner's death
        # branch (seek_resurrection), so we report a plain INTERRUPTED failure
        # with no re-hunt/bandage suggestion to chain off of.
        return ProcedureResult(
            success=not died and (not retreated or kills > 0),
            reason=(
                FailureReason.INTERRUPTED
                if died or (retreated and kills == 0)
                else None
            ),
            message=(
                f"Combat: {kills} kills, +{gained:.1f} weapon/tactics"
                + (f", looted {gold_looted} gold" if gold_looted else "")
                + (" (retreated low HP)" if retreated else "")
                + (" (died — yielding to death recovery)" if died else "")
                + (" (leashed back to anchor)" if leashed else "")
            ),
            skill_gains={SKILL_SWORDS: gained} if gained else {},
            next_suggestion=(
                None if died else ("bandage_self" if retreated else "hunt_nearby")
            ),
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
# When wander_for_combat yields (the spot is swept clean of hostiles) the
# planner falls through to the adventurer loop's productive tail (sell_to_vendor
# / the mining chain). But can_start fires again the very next tick (still armed,
# still no target), so without a cooldown the agent re-enters wandering after a
# SINGLE productive procedure invocation — a wander↔work flap that defeats the
# yield. Suppress wander for this window so the productive work actually runs;
# the open world keeps respawning, so combat resumes once the cooldown lapses.
WANDER_YIELD_COOLDOWN_S = 30.0


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
        if not _initiates_combat(ctx):
            return False  # pacifist persona never roams to find a fight
        # Post-yield cooldown: we already swept this spot clean and handed the
        # tick to productive work — stay off until the cooldown lapses so that
        # work isn't yanked away after one invocation (the wander↔work flap).
        if time.time() < ctx.blackboard.get("_wander_cooldown_until", 0.0):
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
        # `combat_anchor` is set by HuntNearby.execute when a fight starts — but
        # the whole point of this procedure is the case where NO fight has
        # started yet (armed, no target in range), so on a fresh spot the key is
        # absent. A plain `.get(..., (ss.x, ss.y))` then re-derived the orbit
        # center from the agent's CURRENT position every sweep; since go_to walks
        # the agent toward the perimeter tile, the center followed the drift and
        # the warrior wandered unboundedly away from the arena instead of
        # orbiting it. setdefault PINS the anchor on the first wander so every
        # subsequent sweep orbits one fixed center (identical behaviour once a
        # fight has already set the anchor).
        ax, ay = ctx.blackboard.setdefault("combat_anchor", (ss.x, ss.y))
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
            # A fight is back on — drop any pending cooldown so the agent can
            # keep hunting without waiting out a stale yield window.
            ctx.blackboard["_wander_cooldown_until"] = 0.0
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
            # Arm the cooldown so can_start stays off for WANDER_YIELD_COOLDOWN_S:
            # without it, can_start (armed + no target) re-fires next tick and the
            # agent flaps back into wandering after one productive invocation.
            ctx.blackboard["_wander_cooldown_until"] = (
                time.time() + WANDER_YIELD_COOLDOWN_S
            )
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
