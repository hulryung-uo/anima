"""LLM-driven thinking: goal-oriented autonomous decision making."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import structlog

from anima.action.movement import wander_action
from anima.brain.prompt import build_system_prompt
from anima.client.packets import build_double_click, build_unicode_speech, build_walk_request
from anima.data import item_name
from anima.map import FLAG_DOOR
from anima.memory.retrieval import retrieve_context
from anima.memory.rewards import get_reward
from anima.pathfinding import direction_to, find_path
from anima.world_knowledge import find_location, format_locations_for_llm

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext, Status

logger = structlog.get_logger()

THINK_COOLDOWN = 15.0
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
    if goal:
        return f"Current goal: {goal['description']} (heading to {goal.get('place', 'unknown')})"
    return "You have no current goal. Pick something to do."


async def llm_think(ctx: BrainContext) -> Status:
    """LLM-driven decision making with goal persistence."""
    from anima.brain.behavior_tree import Status

    if ctx.llm is None:
        return await wander_action(ctx)

    now = time.time()
    last_think = ctx.blackboard.get("last_think_time", 0.0)

    # Pause during active conversation
    last_player_speech = ctx.blackboard.get("last_player_speech", 0.0)
    in_conversation = (now - last_player_speech) < CONVERSATION_TIMEOUT
    if in_conversation and not ctx.blackboard.get("pending_speech"):
        return Status.SUCCESS

    # If we have an active goal with a move target, keep walking
    move_target = ctx.blackboard.get("move_target")
    if move_target is not None:
        tx, ty = move_target
        sx = ctx.perception.self_state.x
        sy = ctx.perception.self_state.y
        if abs(sx - tx) <= 2 and abs(sy - ty) <= 2:
            # Arrived at destination
            goal = ctx.blackboard.pop("current_goal", None)
            del ctx.blackboard["move_target"]
            _clear_path_cache(ctx)
            place = goal["place"] if goal else "destination"
            logger.info("goal_arrived", place=place, pos=f"({sx},{sy})")
            await _record_episode(
                ctx,
                "go",
                place,
                "success",
                get_reward("goal_arrived"),
                summary=f"Arrived at {place}",
            )
            ctx.blackboard["last_think_time"] = now - THINK_COOLDOWN + 2.0
        elif ctx.walker.can_walk():
            # Check if we're stuck
            stuck = ctx.walker.check_stuck((tx, ty))
            if stuck == "cooldown":
                goal = ctx.blackboard.pop("current_goal", None)
                ctx.blackboard.pop("move_target", None)
                _clear_path_cache(ctx)
                place = goal["place"] if goal else "unknown"
                ctx.walker.last_step_time = (
                    asyncio.get_event_loop().time() * 1000 + 5000
                )
                logger.warning(
                    "movement_stuck_cooldown",
                    target=f"({tx},{ty})",
                    denials=ctx.walker.consecutive_denials,
                )
                await _record_episode(
                    ctx, "go", place, "failure",
                    get_reward("goal_failed"),
                    summary=f"Stuck near {place}: too many walk denials",
                )
                return Status.RUNNING
            elif stuck == "wander":
                ctx.blackboard.pop("move_target", None)
                _clear_path_cache(ctx)
                logger.info(
                    "movement_stuck_wander",
                    target=f"({tx},{ty})",
                    denials=ctx.walker.consecutive_denials,
                )
                return await wander_action(ctx)
            return await _step_toward(ctx, tx, ty)
        else:
            return Status.RUNNING

    # Cooldown — wander while waiting for next think
    if now - last_think < THINK_COOLDOWN:
        if ctx.blackboard.get("current_goal") and not ctx.blackboard.get("move_target"):
            ctx.blackboard.pop("current_goal", None)
        if ctx.blackboard.get("current_goal") is None:
            return await wander_action(ctx)
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
        return await wander_action(ctx)

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
        return await wander_action(ctx)

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
            ctx.blackboard["current_goal"] = {
                "place": loc.name,
                "description": reason or f"Going to {loc.name}",
                "x": loc.x,
                "y": loc.y,
            }
            ctx.blackboard["move_target"] = (loc.x, loc.y)
            _clear_path_cache(ctx)
            ctx.walker.consecutive_denials = 0
            logger.info("goal_set", place=loc.name, target=f"({loc.x},{loc.y})")
            if ctx.walker.can_walk():
                return await _step_toward(ctx, loc.x, loc.y)
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
            return await wander_action(ctx)

    elif act == "speak":
        await _record_episode(ctx, "speak", say[:50], "success", 0.0)
        return Status.SUCCESS

    elif act == "idle":
        return Status.SUCCESS

    else:
        # explore
        await _record_episode(ctx, "explore", "", "success", 0.0)
        return await wander_action(ctx)


# ------------------------------------------------------------------
# Path caching helpers
# ------------------------------------------------------------------

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
    """Collect (x, y) of ground-level world items that may block movement.

    Many UO items (furniture, chairs, etc.) lack the IMPASSABLE flag in tiledata
    but still block movement server-side. We treat all non-container ground items
    as potential obstacles, excluding surfaces/bridges you can walk on.
    """
    blocked: set[tuple[int, int]] = set()
    for it in ctx.perception.world.items.values():
        if it.container != 0:
            continue  # skip contained items (in bags, etc.)
        if it.serial & 0x40000000 == 0:
            continue  # not an item serial (items have high bit set)
        blocked.add((it.x, it.y))
    return blocked


# ------------------------------------------------------------------
# Door detection
# ------------------------------------------------------------------

def _find_door_at(ctx: BrainContext, x: int, y: int) -> int | None:
    """Find a closed door world item at (x, y). Returns serial or None."""
    if ctx.map_reader is None:
        return None
    for it in ctx.perception.world.items.values():
        if it.container != 0:
            continue
        if it.x == x and it.y == y:
            flags = ctx.map_reader._get_item_flags(it.graphic)
            if flags & FLAG_DOOR:
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
        return await wander_action(ctx)

    # Try cached path first
    path = _get_cached_path(ctx, sx, sy, tx, ty)

    if not path:
        # Compute new path, avoiding denied tiles and dynamic obstacles
        denied = set(ctx.walker.denied_tiles.keys()) | _impassable_world_items(ctx)
        sz = ctx.perception.self_state.z
        path = find_path(
            ctx.map_reader, sx, sy, tx, ty,
            max_steps=100, denied_tiles=denied, current_z=sz,
        )
        if not path:
            goal = ctx.blackboard.pop("current_goal", None)
            ctx.blackboard.pop("move_target", None)
            _clear_path_cache(ctx)
            place = goal["place"] if goal else "unknown"
            await _record_episode(
                ctx,
                "go",
                place,
                "failure",
                get_reward("goal_failed"),
                summary=f"No path to {place}",
            )
            return await wander_action(ctx)

    # Cache the path
    ctx.blackboard["cached_path"] = path
    ctx.blackboard["cached_path_target"] = (tx, ty)

    next_x, next_y = path[0]
    direction = direction_to(sx, sy, next_x, next_y)

    # Check for doors at the next tile
    door_serial = _find_door_at(ctx, next_x, next_y)
    if door_serial is not None:
        await ctx.conn.send_packet(build_double_click(door_serial))
        ctx.walker.clear_denied_tile(next_x, next_y)
        logger.info("door_opening", serial=f"0x{door_serial:08X}", pos=f"({next_x},{next_y})")
        return Status.RUNNING

    # Record pending step for denial tracking (both turns and steps send walk packets)
    ctx.walker._pending_step_tile = (next_x, next_y)

    # Turn first if needed
    current_dir = ctx.perception.self_state.direction
    if current_dir != direction:
        seq = ctx.walker.next_sequence()
        fastwalk = ctx.walker.pop_fast_walk_key()
        pkt = build_walk_request(direction, seq, fastwalk)
        await ctx.conn.send_packet(pkt)
        ctx.walker.steps_count += 1
        ctx.walker.last_step_time = (
            asyncio.get_event_loop().time() * 1000 + ctx.cfg.movement.turn_delay_ms
        )
        ctx.perception.self_state.direction = direction
        return Status.SUCCESS

    # Take a step

    seq = ctx.walker.next_sequence()
    fastwalk = ctx.walker.pop_fast_walk_key()
    pkt = build_walk_request(direction, seq, fastwalk)
    await ctx.conn.send_packet(pkt)
    ctx.walker.steps_count += 1
    ctx.walker.last_step_time = (
        asyncio.get_event_loop().time() * 1000 + ctx.cfg.movement.walk_delay_ms
    )

    # Invalidate cache — the first step is consumed
    path_rest = path[1:]
    if path_rest:
        ctx.blackboard["cached_path"] = path_rest
    else:
        _clear_path_cache(ctx)

    return Status.SUCCESS


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
