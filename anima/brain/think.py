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
from anima.pathfinding import direction_to, find_path
from anima.world_knowledge import find_location, format_locations_for_llm

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext, Status

logger = structlog.get_logger()

THINK_COOLDOWN = 30.0  # seconds between LLM think calls (was 15 — too frequent)
CONVERSATION_TIMEOUT = 10.0

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
    if nearby_mobs:
        names = [m.name or "someone" for m in nearby_mobs[:5]]
        lines.append(f"People nearby: {', '.join(names)}")

    return "\n".join(lines) if lines else "Nothing notable nearby."


def _build_recent_speech(ctx: BrainContext) -> str:
    recent = ctx.perception.social.recent(count=3)
    my_serial = ctx.perception.self_state.serial
    lines: list[str] = []
    for entry in recent:
        if entry.name.lower() == "system" or entry.serial == 0xFFFFFFFF:
            continue
        if entry.serial == my_serial:
            lines.append(f'  You: "{entry.text}"')
        else:
            lines.append(f'  {entry.name}: "{entry.text}"')
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

    # Pause during active conversation
    last_player_speech = ctx.blackboard.get("last_player_speech", 0.0)
    in_conversation = (now - last_player_speech) < CONVERSATION_TIMEOUT
    if in_conversation and not ctx.blackboard.get("pending_speech"):
        return Status.SUCCESS

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
                    return Status.SUCCESS
                return await _step_toward(ctx, tx, ty)
            else:
                return Status.RUNNING

        # goal exists but no move_target and can't restore — abandon
        _finish_goal(ctx, goal, "failure")

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

    act = action.get("action", "explore")
    reason = action.get("reason", "")
    say = action.get("say", "").strip()

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
            pub(ctx, "brain.goal_set", f"Goal: go to {loc.name}", importance=2)
            # Use approach point for indoor locations
            nav_x, nav_y = loc.nav_x, loc.nav_y
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

    else:
        # explore
        await _record_episode(ctx, "explore", "", "success", 0.0)
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
        return trimmed if trimmed else None
    except ValueError:
        # Current position not on cached path — might be 1 step ahead
        if cached_path and abs(sx - cached_path[0][0]) <= 1 and abs(sy - cached_path[0][1]) <= 1:
            return cached_path
        return None


# ------------------------------------------------------------------
# Dynamic obstacle detection
# ------------------------------------------------------------------

def _impassable_world_items(ctx: BrainContext) -> set[tuple[int, int]]:
    """Collect (x, y) of ground-level world items that actually have the IMPASSABLE flag."""
    if ctx.map_reader is None:
        return set()
    blocked: set[tuple[int, int]] = set()
    for it in ctx.perception.world.items.values():
        if it.container != 0:
            continue
        if it.serial & 0x40000000 == 0:
            continue
        flags = ctx.map_reader._get_item_flags(it.graphic)
        if flags & FLAG_IMPASSABLE:
            blocked.add((it.x, it.y))
    return blocked


def _scan_building_walls(ctx: BrainContext, radius: int = 20) -> set[tuple[int, int]]:
    """Pre-scan map statics near agent for impassable tiles (building walls, etc).

    This helps A* avoid buildings from the start instead of discovering them
    one tile at a time during pathfinding.
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
            | _scan_building_walls(ctx, radius=25)
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
            # No path found — don't abandon goal, let the main loop retry
            goal = ctx.blackboard.get("current_goal")
            place = goal["place"] if goal else "unknown"
            logger.info(
                "step_toward_no_path",
                pos=f"({sx},{sy},{sz})", target=f"({tx},{ty})", place=place,
            )
            return Status.SUCCESS

    # Cache the path
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


def _parse_action(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
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
                            return json.loads(text[idx : i + 1])
                        except json.JSONDecodeError:
                            break
        else:
            content_start = idx + len(start)
            assert end is not None
            end_idx = text.find(end, content_start)
            if end_idx != -1:
                try:
                    return json.loads(text[content_start:end_idx].strip())
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
    nearby_mobs = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=18)

    has_players = any(m.notoriety is not None and m.notoriety.value <= 6 for m in nearby_mobs)

    if ss.hp_percent < 30:
        return "low_hp"
    if has_players:
        return "near_player"
    return "exploring"
