"""LLM-driven thinking: goal-oriented autonomous decision making."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import structlog

# wander_action disabled — agent stays still instead of random walking
from anima.brain.prompt import build_system_prompt
from anima.client.packets import build_double_click, build_unicode_speech, build_walk_request
from anima.data import item_name
from anima.map import FLAG_DOOR, FLAG_IMPASSABLE
from anima.memory.retrieval import retrieve_context
from anima.memory.rewards import get_reward
from anima.perception.enums import MessageType, NotorietyFlag
from anima.pathfinding import direction_to, find_path, path_is_traversable
from anima.world_knowledge import find_location, format_locations_for_llm

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext, Status

logger = structlog.get_logger()

THINK_COOLDOWN = 30.0  # seconds between LLM think calls (was 15 — too frequent)
CONVERSATION_TIMEOUT = 10.0
# When an LLM think call yields no usable text (timeout / transient backend
# error → LLMResponse(text="")), we must NOT consume the whole THINK_COOLDOWN:
# no decision was actually made.  Roll last_think_time back so the next tick
# retries after only this short backoff instead of going brain-dead for 30s.
THINK_RETRY_BACKOFF = 3.0

THINK_PROMPT = """\
Position: ({x}, {y}).

{locations}

{surroundings}

{current_goal}

{recent_speech}

Decide what to do. Reply with ONE JSON object:
{{"action": "go", "place": "<place name>", "reason": "<why>", "say": ""}}
{{"action": "explore", "reason": "<why>", "say": ""}}
{{"action": "speak", "say": "<text>"}}
{{"action": "idle", "say": ""}}

Rules:
- "go" to a named place from the list above. You will walk there automatically.
- "explore" to wander and discover. Use when you don't know where to go.
- "speak" only if someone is nearby and you have something to say.
- "idle" to stay put and observe.
- Have a PURPOSE. Don't wander aimlessly. Pick a place and go there.
- If you already have a goal and haven't reached it, stick with it.
- "say" should be "" most of the time. Only speak when it matters."""


# Notoriety values that mark a mobile as a THREAT (gray/criminal/orange/red).
# Rendering these as friendly "People nearby" gave the LLM a dangerously wrong
# picture: a red murderer or an aggressive monster read identically to a town
# baker, so the strategist never registered danger when deciding go/idle/speak.
_HOSTILE_NOTORIETY = frozenset(
    {NotorietyFlag.ATTACKABLE, NotorietyFlag.CRIMINAL, NotorietyFlag.ENEMY, NotorietyFlag.MURDERER}
)


def _build_surroundings(ctx: BrainContext) -> str:
    """Build a description of what Anima can see."""
    ss = ctx.perception.self_state
    lines: list[str] = []

    nearby_items = ctx.perception.world.nearby_items(ss.x, ss.y, distance=18)
    if nearby_items:
        seen: set[str] = set()
        landmarks: list[str] = []
        for it in nearby_items:
            name = it.name or item_name(it.graphic)
            if not name or name in seen:
                continue
            seen.add(name)
            landmarks.append(f"  - {name}")
            if len(landmarks) >= 6:
                break
        if landmarks:
            lines.append("Nearby objects: " + ", ".join(s.strip("- ") for s in landmarks))

    nearby_mobs = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=18)
    # Split living mobiles into threats vs. friendly presences. A corpse/ghost
    # is neither, so it must not pad either list (it would make the agent think
    # there are people to talk to, or threats to flee, when there is only a
    # body). Hostiles get their own line so the LLM actually registers danger.
    hostiles = [m for m in nearby_mobs if not m.is_dead and m.notoriety in _HOSTILE_NOTORIETY]
    friendlies = [
        m for m in nearby_mobs if not m.is_dead and m.notoriety not in _HOSTILE_NOTORIETY
    ]
    if hostiles:
        names = [m.name or "something" for m in hostiles[:5]]
        lines.append(f"Hostile nearby: {', '.join(names)}")
    if friendlies:
        names = [m.name or "someone" for m in friendlies[:5]]
        lines.append(f"People nearby: {', '.join(names)}")

    return "\n".join(lines) if lines else "Nothing notable nearby."


# Message types that are NOT actual conversation and must never be surfaced to
# the LLM as "Recent conversation": server SYSTEM lines, single-click LABEL
# responses ("Hastin the baker"), and FOCUS prompts. Everything else (REGULAR,
# EMOTE, WHISPER, YELL, SPELL mantras, GUILD/ALLIANCE/PARTY) is real speech a
# person uttered and is fair game for the conversational window.
_NON_CONVERSATIONAL_TYPES = frozenset(
    {MessageType.SYSTEM, MessageType.LABEL, MessageType.FOCUS}
)
_MAX_CONVERSATION_LINES = 3


def _build_recent_speech(ctx: BrainContext) -> str:
    # Pull a wider window and drop non-conversational entries FIRST, then keep
    # the last few real lines. Slicing to the last 3 *before* filtering meant a
    # burst of system/cliloc/label noise in the final 3 slots could shut out a
    # player's actual message that arrived just before it — the LLM would then
    # see "no conversation" and never reply. Classification is by the
    # authoritative msg_type, not a brittle name == "system" string check
    # (a vendor-named cliloc line, e.g. name="Hastin", slipped through that).
    recent = ctx.perception.social.recent(count=20)
    my_serial = ctx.perception.self_state.serial
    lines: list[str] = []
    for entry in recent:
        if entry.serial == 0xFFFFFFFF:
            continue  # broadcast/system pseudo-serial
        if entry.msg_type in _NON_CONVERSATIONAL_TYPES:
            continue
        if entry.name.lower() == "system":
            continue
        if entry.serial == my_serial:
            lines.append(f'  You: "{entry.text}"')
        else:
            lines.append(f'  {entry.name}: "{entry.text}"')
    lines = lines[-_MAX_CONVERSATION_LINES:]
    if lines:
        return "Recent conversation:\n" + "\n".join(lines)
    return ""


def _build_goal_context(ctx: BrainContext) -> str:
    goal = ctx.blackboard.get("current_goal")
    parts: list[str] = []
    if goal:
        place = goal.get("place", "unknown")
        parts.append(f"Current goal: {goal['description']} (heading to {place})")
    else:
        parts.append("You have no current goal. Pick something to do.")

    # Include skill problems if any
    problem = ctx.blackboard.pop("skill_problem", None)
    if problem:
        parts.append(f"PROBLEM: {problem}")

    # Include inventory/economy status
    ss = ctx.perception.self_state
    if ss.gold > 0:
        parts.append(f"Gold: {ss.gold}gp")
        if ss.gold >= 500:
            parts.append("TIP: You have a lot of gold. Consider depositing at the bank.")

    if ss.weight_max > 0:
        pct = ss.weight / ss.weight_max * 100
        parts.append(f"Weight: {ss.weight}/{ss.weight_max} ({pct:.0f}%)")
        if pct > 80:
            parts.append(
                "WARNING: Too heavy! Go sell items at a shop or deposit at bank."
            )

    return "\n".join(parts)


async def llm_think(ctx: BrainContext) -> Status:
    """LLM-driven decision making with goal persistence."""
    from anima.brain.behavior_tree import Status

    if ctx.llm is None:
        return Status.SUCCESS

    now = time.time()
    last_think = ctx.blackboard.get("last_think_time", 0.0)

    # An active conversation pauses *fresh* strategising (don't wander off /
    # re-decide while someone is talking to us) — but it must NOT freeze a walk
    # already in progress. The gate used to sit ahead of the goal block, so any
    # nearby utterance (including a hostile mob's, which also stamps
    # last_player_speech) stranded the agent mid-route for the whole
    # CONVERSATION_TIMEOUT window. Evaluate it here and apply it only to the
    # new-think path below, after the active-goal movement block has had its
    # turn to keep stepping toward the destination.
    last_player_speech = ctx.blackboard.get("last_player_speech", 0.0)
    in_conversation = (now - last_player_speech) < CONVERSATION_TIMEOUT

    # ---- Active goal: keep pursuing until done or definitively failed ----
    goal = ctx.blackboard.get("current_goal")
    move_target = ctx.blackboard.get("move_target")

    if goal:
        sx = ctx.perception.self_state.x
        sy = ctx.perception.self_state.y

        # Restore move_target if lost (pathfinding failure cleared it)
        if move_target is None:
            loc = find_location(goal["place"])
            if loc:
                move_target = (loc.nav_x, loc.nav_y)
                ctx.blackboard["move_target"] = move_target
                _clear_path_cache(ctx)

        if move_target is not None:
            tx, ty = move_target

            if abs(sx - tx) <= 2 and abs(sy - ty) <= 2:
                place = goal["place"]

                # If arrived at approach point, try entering building
                if not goal.get("_entered"):
                    loc = find_location(place)
                    if (loc and loc.approach_x is not None
                            and (loc.x != loc.nav_x or loc.y != loc.nav_y)):
                        inner_x, inner_y = loc.x, loc.y
                        if abs(sx - inner_x) > 2 or abs(sy - inner_y) > 2:
                            goal["_entered"] = True
                            ctx.blackboard["move_target"] = (inner_x, inner_y)
                            _clear_path_cache(ctx)
                            logger.info(
                                "entering_building", place=place,
                                inner=f"({inner_x},{inner_y})",
                            )
                            if ctx.walker.can_walk():
                                return await _step_toward(ctx, inner_x, inner_y)
                            return Status.RUNNING

                # Actually arrived — clear goal, allow next think
                _finish_goal(ctx, goal, "success")
                ctx.blackboard["last_think_time"] = now - THINK_COOLDOWN + 2.0

            elif ctx.walker.can_walk():
                # Try opening closed doors on denied tiles
                for dx, dy in list(ctx.walker.denied_tiles.keys())[:10]:
                    door = _find_closed_door_at(ctx, dx, dy)
                    if door is not None:
                        logger.info("opening_door_on_deny", pos=f"({dx},{dy})")
                        await ctx.conn.send_packet(build_double_click(door))
                        ctx.walker.clear_denied_tile(dx, dy)
                        _clear_path_cache(ctx)
                        await asyncio.sleep(0.5)
                        break

                stuck = ctx.walker.check_stuck((tx, ty))
                if stuck == "cooldown":
                    # Stuck — retry with cleared denied tiles, don't abandon goal
                    retries = goal.get("_stuck_retries", 0) + 1
                    goal["_stuck_retries"] = retries
                    ctx.walker.last_step_time = (
                        asyncio.get_event_loop().time() * 1000 + 3000
                    )
                    _clear_path_cache(ctx)
                    logger.warning(
                        "goal_stuck_retry", place=goal["place"],
                        target=f"({tx},{ty})", retry=retries,
                        denials=ctx.walker.consecutive_denials,
                    )
                    # Give up after too many retries
                    if retries >= 5:
                        logger.warning("goal_stuck_give_up", place=goal["place"])
                        _finish_goal(ctx, goal, "failure")
                    return Status.RUNNING
                elif stuck == "wander":
                    # Briefly stuck — clear path cache and retry, keep goal
                    _clear_path_cache(ctx)
                    ctx.walker.consecutive_denials = 0
                    return Status.SUCCESS
                return await _step_toward(ctx, tx, ty)
            else:
                return Status.RUNNING

        # goal exists but no move_target and can't restore — abandon
        _finish_goal(ctx, goal, "failure")

    # Someone is talking to us and we have no goal to keep pursuing — wait and
    # observe rather than starting a fresh think (which could pick a new place
    # and walk away mid-conversation). A pending reply is handled by the social
    # branch, not here, so it skips this wait.
    if in_conversation and not ctx.blackboard.get("pending_speech"):
        return Status.SUCCESS

    # If skills are succeeding, don't rethink
    if ctx.blackboard.get("skill_consecutive_fails", 0) == 0:
        last_skill = ctx.blackboard.get("last_skill_time", 0.0)
        if now - last_skill < 10.0:
            return Status.FAILURE

    # Cooldown between thinks
    if now - last_think < THINK_COOLDOWN:
        return Status.SUCCESS

    # Time to think
    ctx.blackboard["last_think_time"] = now
    ss = ctx.perception.self_state

    # Retrieve memory context
    memory_block = await retrieve_context(ctx)

    system = build_system_prompt(ctx, memory_block=memory_block)
    user_msg = THINK_PROMPT.format(
        x=ss.x,
        y=ss.y,
        locations=format_locations_for_llm(ss.x, ss.y),
        surroundings=_build_surroundings(ctx),
        current_goal=_build_goal_context(ctx),
        recent_speech=_build_recent_speech(ctx),
    )

    result = await ctx.llm.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user_msg}]
    )
    if not result.text:
        # Empty response = no decision. Don't waste the full cooldown — schedule
        # a near-term retry so a flaky backend can't idle the agent for 30s.
        ctx.blackboard["last_think_time"] = now - THINK_COOLDOWN + THINK_RETRY_BACKOFF
        logger.warning("think_empty_response", retry_in_s=THINK_RETRY_BACKOFF)
        return Status.SUCCESS

    # Record LLM thinking to journal (if model supports extended thinking)
    if result.thinking:
        journal = ctx.blackboard.get("journal")
        if journal is not None:
            await journal.record_event(
                narrative=f"[생각] {result.thinking[:500]}",
                category="thinking",
                action="llm_think",
                x=ss.x,
                y=ss.y,
                importance=2,
            )

    action = _parse_action(result.text)
    if action is None:
        logger.warning("think_parse_failed", raw=result.text[:100])
        return Status.SUCCESS

    # dict.get(..., default) only substitutes the default for MISSING keys, not
    # for keys present with a JSON ``null`` value. LLMs routinely emit
    # ``{"action": "speak", "say": null}`` (or null reason/action), which would
    # make ``.get("say", "")`` return ``None`` and ``None.strip()`` raise
    # AttributeError — crashing the whole think tick. Coerce null/non-string
    # values back to their empty/default form so dispatch stays well-typed.
    act = action.get("action") or "explore"
    reason = action.get("reason") or ""
    say = (action.get("say") or "").strip()

    logger.info(
        "think_decided",
        action=act,
        reason=reason[:60],
        say=say[:50],
        duration_ms=f"{result.total_duration_ms:.0f}",
    )

    from anima.core.publish import pub
    pub(ctx, "brain.think", f"Think: {act} — {reason[:60]}", importance=2,
        action=act, reason=reason)

    # Speak if warranted
    if say and not ctx.blackboard.get("pending_speech"):
        recent = ctx.perception.social.recent(count=3)
        my_serial = ctx.perception.self_state.serial
        already_said = any(e.serial == my_serial and e.text.lower() == say.lower() for e in recent)
        if not already_said:
            await ctx.conn.send_packet(build_unicode_speech(say[:200]))
            logger.info("think_speak", text=say[:200])

    # Execute action
    if act == "go":
        place_name = action.get("place", "")
        loc = find_location(place_name)
        if loc:
            # Use approach point for indoor locations
            nav_x, nav_y = loc.nav_x, loc.nav_y

            # Already at destination — don't set a goal that completes instantly
            if abs(ss.x - nav_x) <= 2 and abs(ss.y - nav_y) <= 2:
                logger.info("already_at_goal", place=loc.name, pos=f"({ss.x},{ss.y})")
                pub(ctx, "brain.think", f"Already at {loc.name}, looking for work", importance=1)
                return Status.SUCCESS

            pub(ctx, "brain.goal_set", f"Goal: go to {loc.name}", importance=2)
            ctx.blackboard["current_goal"] = {
                "place": loc.name,
                "description": reason or f"Going to {loc.name}",
                "x": nav_x,
                "y": nav_y,
            }
            ctx.blackboard["move_target"] = (nav_x, nav_y)
            _clear_path_cache(ctx)
            ctx.walker.consecutive_denials = 0
            logger.info("goal_set", place=loc.name, target=f"({nav_x},{nav_y})")
            if ctx.walker.can_walk():
                return await _step_toward(ctx, nav_x, nav_y)
            return Status.RUNNING
        else:
            logger.warning("goal_place_unknown", place=place_name)
            await _record_episode(
                ctx,
                "go",
                place_name,
                "failure",
                get_reward("goal_failed"),
                summary=f"Unknown place: {place_name}",
            )
            return Status.SUCCESS

    elif act == "speak":
        await _record_episode(ctx, "speak", say[:50], "success", 0.0)
        return Status.SUCCESS

    elif act == "idle":
        return Status.SUCCESS

    elif act == "explore":
        # Wander randomly — for now just let skill_exec pick something
        logger.info("think_explore", reason=reason)
        return Status.SUCCESS

    else:
        # LLM may have output a skill name (e.g. "mine_ore") instead of
        # a valid action.  Check if it's a known skill and diagnose why
        # it can't run, so the *next* think call gets actionable feedback.
        from anima.skills.base import SkillRegistry
        registry: SkillRegistry | None = ctx.blackboard.get("skill_registry")
        if registry:
            skill = registry.get(act)
            if skill:
                reason_str = await skill.diagnose(ctx)
                if reason_str:
                    ctx.blackboard["skill_problem"] = (
                        f"You tried '{act}' but it cannot execute: {reason_str}. "
                        "Use a valid action: go/explore/idle. "
                        "Go buy tools or move to the right location."
                    )
                    logger.info("think_skill_blocked", skill=act, reason=reason_str)
                else:
                    # Skill IS available — skill_exec will handle it next tick
                    logger.info("think_skill_deferred", skill=act)
                return Status.SUCCESS

        logger.info("think_action_passthrough", action=act, reason=reason)
        return Status.SUCCESS


# ------------------------------------------------------------------
# Path caching helpers
# ------------------------------------------------------------------

def _finish_goal(ctx: BrainContext, goal: dict, outcome: str) -> None:
    """Clean up a completed or failed goal."""
    place = goal.get("place", "unknown")
    ctx.blackboard.pop("current_goal", None)
    ctx.blackboard.pop("move_target", None)
    _clear_path_cache(ctx)
    logger.info("goal_finished", place=place, outcome=outcome)


def _clear_path_cache(ctx: BrainContext) -> None:
    ctx.blackboard.pop("cached_path", None)
    ctx.blackboard.pop("cached_path_target", None)


def _get_cached_path(
    ctx: BrainContext, sx: int, sy: int, tx: int, ty: int,
) -> list[tuple[int, int]] | None:
    """Return cached path if still valid, else None."""
    cached_path = ctx.blackboard.get("cached_path")
    cached_target = ctx.blackboard.get("cached_path_target")

    if cached_path is None or cached_target != (tx, ty):
        return None

    # Trim path to current position
    try:
        idx = cached_path.index((sx, sy))
        trimmed = cached_path[idx + 1:]
        candidate = trimmed if trimmed else None
    except ValueError:
        # Current position not on cached path — might be 1 step ahead
        if cached_path and abs(sx - cached_path[0][0]) <= 1 and abs(sy - cached_path[0][1]) <= 1:
            candidate = cached_path
        else:
            candidate = None

    if not candidate:
        return None

    # A tile on the remaining route may have become blocked since the path
    # was planned (mob moved onto it, DenyWalk recorded it). Reusing it would
    # send the agent straight into a now-impassable tile — replan instead.
    if ctx.map_reader is not None:
        denied = set(ctx.walker.denied_tiles.keys()) | _impassable_world_items(ctx)
        sz = ctx.perception.self_state.z
        if not path_is_traversable(
            ctx.map_reader, sx, sy, candidate, denied_tiles=denied, current_z=sz,
        ):
            return None
    return candidate


# ------------------------------------------------------------------
# Dynamic obstacle detection
# ------------------------------------------------------------------

def _impassable_world_items(ctx: BrainContext) -> set[tuple[int, int]]:
    """Collect (x, y) of ground-level world items that block walking.

    Doors are excluded: a closed door carries FLAG_DOOR|FLAG_IMPASSABLE, but
    ``go_to`` opens doors automatically during movement and the pathfinder
    plans through them (``doors_passable=True``). If a door tile leaks into
    this set it is handed to ``path_is_traversable`` as a denied tile, which is
    checked *before* the door-passable logic and short-circuits to False —
    needlessly discarding every cached path that legitimately routes through a
    closed door. Mirror movement.py's twin and keep doors out.
    """
    if ctx.map_reader is None:
        return set()
    blocked: set[tuple[int, int]] = set()
    for it in ctx.perception.world.items.values():
        if it.container != 0:
            continue
        if it.serial & 0x40000000 == 0:
            continue
        flags = ctx.map_reader._get_item_flags(it.graphic)
        if (flags & FLAG_IMPASSABLE) and not (flags & FLAG_DOOR):
            blocked.add((it.x, it.y))
    return blocked


def _scan_building_walls(ctx: BrainContext, radius: int = 20) -> set[tuple[int, int]]:
    """Pre-scan map statics near agent for impassable tiles (building walls, etc).

    Uses walkable_z with the agent's current Z so cave floors (static
    surfaces on impassable land) are correctly treated as walkable.
    """
    if ctx.map_reader is None:
        return set()
    ss = ctx.perception.self_state
    sx, sy, sz = ss.x, ss.y, ss.z
    walls: set[tuple[int, int]] = set()

    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            x, y = sx + dx, sy + dy
            tile = ctx.map_reader.get_tile(x, y)
            can, _ = tile.walkable_z(sz)
            if not can:
                walls.add((x, y))

    return walls


# ------------------------------------------------------------------
# Door detection
# ------------------------------------------------------------------

def _find_closed_door_at(ctx: BrainContext, x: int, y: int) -> int | None:
    """Find a CLOSED door world item at or adjacent to (x, y).

    Returns serial or None. Only returns doors that are impassable
    (closed). Open doors have FLAG_DOOR but NOT FLAG_IMPASSABLE —
    the agent can walk through them freely.
    """
    if ctx.map_reader is None:
        return None

    for it in ctx.perception.world.items.values():
        if it.container != 0:
            continue
        if abs(it.x - x) <= 1 and abs(it.y - y) <= 1:
            flags = ctx.map_reader._get_item_flags(it.graphic)
            # Closed door = FLAG_DOOR + FLAG_IMPASSABLE
            # Open door = FLAG_DOOR only (walkable)
            if (flags & FLAG_DOOR) and (flags & FLAG_IMPASSABLE):
                return it.serial

    return None


# ------------------------------------------------------------------
# Core step logic
# ------------------------------------------------------------------

async def _step_toward(ctx: BrainContext, tx: int, ty: int) -> Status:
    """Take a single step toward (tx, ty) using pathfinding with caching."""
    from anima.brain.behavior_tree import Status

    sx = ctx.perception.self_state.x
    sy = ctx.perception.self_state.y

    if not ctx.walker.can_walk():
        return Status.RUNNING

    if ctx.map_reader is None:
        return Status.SUCCESS

    # Invalidate path cache if walker was denied
    if ctx.walker._path_dirty:
        _clear_path_cache(ctx)
        ctx.walker._path_dirty = False

    # Try cached path first
    path = _get_cached_path(ctx, sx, sy, tx, ty)

    if not path:
        denied = (
            set(ctx.walker.denied_tiles.keys())
            | _impassable_world_items(ctx)
        )
        sz = ctx.perception.self_state.z

        # If destination is far, aim for an intermediate waypoint
        dist = max(abs(tx - sx), abs(ty - sy))
        if dist > 80:
            ratio = 60.0 / dist
            mid_x = int(sx + (tx - sx) * ratio)
            mid_y = int(sy + (ty - sy) * ratio)
            path = find_path(
                ctx.map_reader, sx, sy, mid_x, mid_y,
                max_steps=1500, denied_tiles=denied, current_z=sz,
            )
        else:
            path = find_path(
                ctx.map_reader, sx, sy, tx, ty,
                max_steps=2000, denied_tiles=denied, current_z=sz,
            )

        if not path:
            goal = ctx.blackboard.get("current_goal")
            place = goal["place"] if goal else "unknown"
            no_path_count = ctx.blackboard.get("_no_path_count", 0) + 1
            ctx.blackboard["_no_path_count"] = no_path_count
            logger.info(
                "step_toward_no_path",
                pos=f"({sx},{sy},{sz})", target=f"({tx},{ty})",
                place=place, attempt=no_path_count,
            )
            if no_path_count >= 3 and goal:
                _finish_goal(ctx, goal, "failure")
                ctx.blackboard.pop("_no_path_count", None)
            return Status.SUCCESS

    # Path found — reset failure counter and cache
    ctx.blackboard.pop("_no_path_count", None)
    ctx.blackboard["cached_path"] = path
    ctx.blackboard["cached_path_target"] = (tx, ty)

    # UO movement: if facing different direction, first packet turns only.
    # Second packet (same direction) actually moves one tile.
    # Send up to MAX_STEP_COUNT packets per tick.
    steps_sent = 0
    cx, cy = sx, sy
    current_dir = ctx.perception.self_state.direction
    remaining_path = list(path)

    while remaining_path and ctx.walker.can_walk():
        next_x, next_y = remaining_path[0]
        direction = direction_to(cx, cy, next_x, next_y)

        # Check for closed doors at the next tile — open them
        door_serial = _find_closed_door_at(ctx, next_x, next_y)
        if door_serial is not None:
            logger.debug("opening_door", serial=f"0x{door_serial:08X}", pos=f"({next_x},{next_y})")
            await ctx.conn.send_packet(build_double_click(door_serial))
            ctx.walker.clear_denied_tile(next_x, next_y)
            await asyncio.sleep(0.5)  # wait for server to update door graphic

        is_turn = (current_dir != direction)

        if is_turn:
            # A turn-only walk packet carries NO position. If we leave
            # _pending_step_tile pointing at the next tile, the turn's own
            # ConfirmWalk (0x22) — whose seq matches the one stamped below —
            # makes walker.confirm_walk() snap the avatar onto a tile it only
            # *turned* toward but never walked onto, desyncing SelfState from
            # the server and corrupting every subsequent re-path from the
            # phantom origin. Route the facing through _pending_direction so it
            # is applied on the turn's confirm instead. go_to / wander_action /
            # _walk_one_step already guard this; the brain's main per-tick move
            # loop must too.
            ctx.walker._pending_step_tile = None
            ctx.walker._pending_direction = direction
        else:
            ctx.walker._pending_step_tile = (next_x, next_y)
        seq = ctx.walker.next_sequence()
        fastwalk = ctx.walker.pop_fast_walk_key()
        pkt = build_walk_request(direction, seq, fastwalk)
        await ctx.conn.send_packet(pkt)
        ctx.walker.steps_count += 1
        steps_sent += 1

        if is_turn:
            # Turn only — no delay, immediately send step in same direction
            current_dir = direction
            ctx.perception.self_state.direction = direction
            # Don't update last_step_time so can_walk() stays True
        else:
            # Actual move — apply walk delay
            ctx.walker.last_step_time = (
                asyncio.get_event_loop().time() * 1000
                + ctx.cfg.movement.walk_delay_ms
            )
            cx, cy = next_x, next_y
            remaining_path.pop(0)

    # Update path cache
    if remaining_path:
        ctx.blackboard["cached_path"] = remaining_path
    else:
        _clear_path_cache(ctx)

    return Status.SUCCESS if steps_sent > 0 else Status.RUNNING


def _coerce_action_dict(value: object) -> dict | None:
    """Normalize a parsed JSON value into an action dict, or None.

    LLMs frequently wrap the action object in a list (``[{...}]``) or emit a
    bare scalar/string.  The caller treats the result as a dict and calls
    ``.get(...)`` on it, so anything that is not a dict must collapse to
    ``None`` here — the ``if action is None`` branch then degrades gracefully
    instead of raising ``AttributeError`` and killing the whole think tick.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        # Take the first dict element of a list-wrapped response.
        for item in value:
            if isinstance(item, dict):
                return item
    return None


def _parse_action(text: str) -> dict | None:
    text = text.strip()
    try:
        parsed = _coerce_action_dict(json.loads(text))
        if parsed is not None:
            return parsed
    except json.JSONDecodeError:
        pass

    # Find JSON in text
    for start, end in [("```json", "```"), ("```", "```"), ("{", None)]:
        idx = text.find(start)
        if idx == -1:
            continue
        if start == "{":
            depth = 0
            for i in range(idx, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = _coerce_action_dict(json.loads(text[idx : i + 1]))
                            if parsed is not None:
                                return parsed
                            break
                        except json.JSONDecodeError:
                            break
        else:
            content_start = idx + len(start)
            assert end is not None
            end_idx = text.find(end, content_start)
            if end_idx != -1:
                try:
                    parsed = _coerce_action_dict(
                        json.loads(text[content_start:end_idx].strip())
                    )
                    if parsed is not None:
                        return parsed
                    continue
                except json.JSONDecodeError:
                    continue
    return None


async def _record_episode(
    ctx: BrainContext,
    action: str,
    target: str,
    outcome: str,
    reward: float,
    summary: str = "",
) -> None:
    """Record an experience episode to memory and update action stats."""
    memory_db = ctx.memory_db
    if memory_db is None:
        return

    agent_name = _agent_name(ctx)
    ss = ctx.perception.self_state

    await memory_db.record_episode(
        agent_name=agent_name,
        location_x=ss.x,
        location_y=ss.y,
        action=action,
        target=target,
        outcome=outcome,
        reward=reward,
        summary=summary,
    )

    # Update action stats
    context_pattern = _infer_context_pattern(ctx)
    await memory_db.update_action_stats(
        agent_name,
        context_pattern,
        action,
        success=(outcome == "success"),
        reward=reward,
    )

    # Feed the location-value map. retrieve_context surfaces a "This area
    # (region X,Y)" block from get_location_values, but nothing in the runtime
    # ever wrote to it — update_location_value had zero callers, so that whole
    # learned-memory channel was permanently empty and the LLM never saw which
    # regions actually paid off for which activity. Every episode already
    # carries a location, an action, and a reward, so key the region tally by
    # the episode action (the "activity") whenever the signal is non-zero;
    # zero-reward episodes (neutral speech, no-op moves) carry no learning
    # signal and would only dilute the per-visit average the read path ranks by.
    if reward != 0.0:
        from anima.skills.state import region_coords

        rx, ry = region_coords(ss.x, ss.y)
        await memory_db.update_location_value(agent_name, rx, ry, action, reward)

    # Trigger reflection periodically
    episode_count = ctx.blackboard.get("episode_count", 0) + 1
    ctx.blackboard["episode_count"] = episode_count
    if episode_count % 20 == 0 and ctx.llm is not None:
        from anima.memory.learning import reflect

        facts = await reflect(memory_db, ctx.llm, agent_name)
        if facts:
            logger.info("reflection_complete", new_facts=len(facts))

    # Prune if needed
    if episode_count % 100 == 0:
        pruned = await memory_db.prune_episodes(agent_name, ctx.cfg.memory.max_episodes)
        if pruned:
            logger.info("episodes_pruned", count=pruned)


def _agent_name(ctx: BrainContext) -> str:
    persona = ctx.blackboard.get("persona")
    return persona.name if persona else "Anima"


def _infer_context_pattern(ctx: BrainContext) -> str:
    """Infer a rough context pattern from the current game state."""
    ss = ctx.perception.self_state
    # Player detection gates on the human body, not notoriety: hostile mobs
    # carry ATTACKABLE(3)/ENEMY(5)/MURDERER(6) notoriety, so a notoriety<=6
    # test buckets every solo grind/combat episode (a field full of monsters)
    # under "near_player" instead of "exploring", corrupting the action-stats
    # reward buckets the LLM later reads back via retrieve_context.
    from anima.skills.state import has_player_nearby
    has_players = has_player_nearby(ctx)

    if ss.hp_percent < 30:
        return "low_hp"
    if has_players:
        return "near_player"
    return "exploring"
