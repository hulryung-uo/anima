"""LLM-driven thinking: goal-oriented autonomous decision making."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import structlog

from anima.action.movement import wander_action
from anima.brain.prompt import build_system_prompt
from anima.client.packets import build_unicode_speech, build_walk_request
from anima.data import item_name
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
            place = goal["place"] if goal else "destination"
            logger.info("goal_arrived", place=place, pos=f"({sx},{sy})")
            # Force a new think cycle soon
            ctx.blackboard["last_think_time"] = now - THINK_COOLDOWN + 2.0
        elif ctx.walker.can_walk():
            return await _step_toward(ctx, tx, ty)
        else:
            return Status.RUNNING

    # Cooldown — wander while waiting for next think
    if now - last_think < THINK_COOLDOWN:
        # If we have no goal, wander slowly; otherwise just wait
        if ctx.blackboard.get("current_goal") is None:
            return await wander_action(ctx)
        return Status.SUCCESS

    # Time to think
    ctx.blackboard["last_think_time"] = now
    ss = ctx.perception.self_state

    system = build_system_prompt(ctx)
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
            logger.info("goal_set", place=loc.name, target=f"({loc.x},{loc.y})")
            if ctx.walker.can_walk():
                return await _step_toward(ctx, loc.x, loc.y)
            return Status.RUNNING
        else:
            logger.warning("goal_place_unknown", place=place_name)
            return await wander_action(ctx)

    elif act == "speak":
        return Status.SUCCESS

    elif act == "idle":
        return Status.SUCCESS

    else:
        # explore
        return await wander_action(ctx)


async def _step_toward(ctx: BrainContext, tx: int, ty: int) -> Status:
    """Take a single step toward (tx, ty) using pathfinding."""
    from anima.brain.behavior_tree import Status

    sx = ctx.perception.self_state.x
    sy = ctx.perception.self_state.y

    if not ctx.walker.can_walk():
        return Status.RUNNING

    if ctx.map_reader is None:
        return await wander_action(ctx)

    path = find_path(ctx.map_reader, sx, sy, tx, ty, max_steps=100)
    if not path:
        ctx.blackboard.pop("move_target", None)
        ctx.blackboard.pop("current_goal", None)
        return await wander_action(ctx)

    next_x, next_y = path[0]
    direction = direction_to(sx, sy, next_x, next_y)

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

    seq = ctx.walker.next_sequence()
    fastwalk = ctx.walker.pop_fast_walk_key()
    pkt = build_walk_request(direction, seq, fastwalk)
    await ctx.conn.send_packet(pkt)
    ctx.walker.steps_count += 1
    ctx.walker.last_step_time = (
        asyncio.get_event_loop().time() * 1000 + ctx.cfg.movement.walk_delay_ms
    )
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
