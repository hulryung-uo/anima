"""LLM-driven thinking: decide what to do next based on world context."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import structlog

from anima.action.movement import wander_action
from anima.brain.prompt import build_system_prompt
from anima.client.packets import build_unicode_speech
from anima.data import item_name

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext, Status

logger = structlog.get_logger()

THINK_COOLDOWN = 15.0  # seconds between LLM decisions
CONVERSATION_TIMEOUT = 10.0  # seconds of silence before resuming exploration

THINK_PROMPT = """\
Position: ({x}, {y}).

{surroundings}

{recent_speech}

What do you do next? Reply with ONE JSON object only:
{{"action": "move", "x": <int>, "y": <int>, "say": ""}}
{{"action": "explore", "say": ""}}
{{"action": "speak", "say": "<text>"}}

Rules:
- MOVE toward interesting places you can see. This is your primary action.
- You may say something short while moving, or stay silent — both are fine.
- If players are nearby, you can greet them or comment on something.
- Do NOT repeat what you already said. Check recent conversation.
- Do NOT keep introducing yourself. Once is enough."""


def _build_surroundings(ctx: BrainContext) -> str:
    """Build a description of what Anima can see."""
    ss = ctx.perception.self_state
    lines: list[str] = []

    # Nearby named items (landmarks, buildings, furniture)
    nearby_items = ctx.perception.world.nearby_items(ss.x, ss.y, distance=18)
    if nearby_items:
        seen: set[str] = set()
        landmarks: list[str] = []
        for item in nearby_items:
            name = item.name or item_name(item.graphic)
            if not name or name in seen:
                continue
            seen.add(name)
            dx, dy = item.x - ss.x, item.y - ss.y
            landmarks.append(f"  - {name} at ({item.x}, {item.y}), {_direction_word(dx, dy)}")
            if len(landmarks) >= 10:
                break
        if landmarks:
            lines.append("Things you can see:")
            lines.extend(landmarks)

    # Nearby mobiles (players only — skip NPCs/vendors with low serials)
    nearby_mobs = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=18)
    players = [m for m in nearby_mobs if m.serial >= 0x00010000]
    if players:
        people: list[str] = []
        for mob in players[:5]:
            name = mob.name or "someone"
            dx, dy = mob.x - ss.x, mob.y - ss.y
            people.append(f"  - {name} at ({mob.x}, {mob.y}), {_direction_word(dx, dy)}")
        lines.append("Players nearby:")
        lines.extend(people)

    if not lines:
        lines.append("You don't see anything particularly interesting nearby.")

    return "\n".join(lines)


def _build_recent_speech(ctx: BrainContext) -> str:
    """Build recent conversation context."""
    recent = ctx.perception.social.recent(count=3)
    my_serial = ctx.perception.self_state.serial
    lines: list[str] = []
    for entry in recent:
        if entry.name.lower() == "system" or entry.serial == 0xFFFFFFFF:
            continue
        if entry.serial == my_serial:
            lines.append(f'  You said: "{entry.text}"')
        else:
            lines.append(f'  {entry.name} said: "{entry.text}"')
    if lines:
        return "Recent conversation:\n" + "\n".join(lines)
    return "No one has spoken to you recently."


def _direction_word(dx: int, dy: int) -> str:
    """Convert dx/dy to a human-readable direction."""
    dist = max(abs(dx), abs(dy))
    if dist <= 3:
        return "right next to you"
    parts = []
    if dy < -3:
        parts.append("to the north")
    elif dy > 3:
        parts.append("to the south")
    if dx > 3:
        parts.append("to the east")
    elif dx < -3:
        parts.append("to the west")
    return " and ".join(parts) if parts else "nearby"


async def llm_think(ctx: BrainContext) -> Status:
    """LLM-driven decision making. Runs on a cooldown."""
    from anima.brain.behavior_tree import Status

    # If no LLM, fall back to random wander
    if ctx.llm is None:
        return await wander_action(ctx)

    now = time.time()
    last_think = ctx.blackboard.get("last_think_time", 0.0)

    # If in active conversation, pause exploration and just wait
    last_player_speech = ctx.blackboard.get("last_player_speech", 0.0)
    in_conversation = (now - last_player_speech) < CONVERSATION_TIMEOUT
    if in_conversation and not ctx.blackboard.get("pending_speech"):
        # Conversation active but no pending reply — just wait, don't wander
        return Status.SUCCESS

    # If we have a pending move target, keep walking toward it
    move_target = ctx.blackboard.get("move_target")
    if move_target is not None:
        tx, ty = move_target
        sx = ctx.perception.self_state.x
        sy = ctx.perception.self_state.y
        if abs(sx - tx) <= 1 and abs(sy - ty) <= 1:
            # Arrived — clear target
            del ctx.blackboard["move_target"]
            logger.info("think_arrived", pos=f"({sx},{sy})")
        elif ctx.walker.can_walk():
            # Take one step toward target
            return await _step_toward(ctx, tx, ty)
        else:
            return Status.RUNNING

    # Cooldown not expired — just wander a step
    if now - last_think < THINK_COOLDOWN:
        return await wander_action(ctx)

    # Time to think!
    ctx.blackboard["last_think_time"] = now

    ss = ctx.perception.self_state
    surroundings = _build_surroundings(ctx)
    recent_speech = _build_recent_speech(ctx)

    system = build_system_prompt(ctx)
    user_msg = THINK_PROMPT.format(
        x=ss.x,
        y=ss.y,
        surroundings=surroundings,
        recent_speech=recent_speech,
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    result = await ctx.llm.chat(messages)
    if not result.text:
        return await wander_action(ctx)

    # Parse LLM response
    action = _parse_action(result.text)
    if action is None:
        logger.warning("think_parse_failed", raw=result.text[:100])
        return await wander_action(ctx)

    logger.info(
        "think_decided",
        action=action.get("action"),
        say=action.get("say", "")[:50],
        duration_ms=f"{result.total_duration_ms:.0f}",
    )

    # Execute speech if present (but skip if there's pending speech to reply to,
    # or if we recently said the same thing)
    say = action.get("say", "").strip()
    if say and not ctx.blackboard.get("pending_speech"):
        # Check for repetition
        recent = ctx.perception.social.recent(count=3)
        my_serial = ctx.perception.self_state.serial
        already_said = any(e.serial == my_serial and e.text.lower() == say.lower() for e in recent)
        if not already_said:
            await ctx.conn.send_packet(build_unicode_speech(say[:200]))
            logger.info("think_speak", text=say[:200])

    # Execute action
    act = action.get("action", "explore")
    if act == "move":
        tx = action.get("x", ss.x)
        ty = action.get("y", ss.y)
        # Clamp to reasonable range (within 20 tiles)
        tx = max(ss.x - 20, min(ss.x + 20, int(tx)))
        ty = max(ss.y - 20, min(ss.y + 20, int(ty)))
        ctx.blackboard["move_target"] = (tx, ty)
        logger.info("think_move", target=f"({tx},{ty})")
        if ctx.walker.can_walk():
            return await _step_toward(ctx, tx, ty)
        return Status.RUNNING
    elif act == "speak":
        # Speech already handled above
        return Status.SUCCESS
    else:
        # explore / unknown — wander
        return await wander_action(ctx)


async def _step_toward(ctx: BrainContext, tx: int, ty: int) -> Status:
    """Take a single step toward (tx, ty) using pathfinding."""
    from anima.brain.behavior_tree import Status
    from anima.pathfinding import direction_to, find_path

    sx = ctx.perception.self_state.x
    sy = ctx.perception.self_state.y

    if not ctx.walker.can_walk():
        return Status.RUNNING

    if ctx.map_reader is None:
        return await wander_action(ctx)

    # Find path and take first step
    path = find_path(ctx.map_reader, sx, sy, tx, ty, max_steps=50)
    if not path:
        # Can't reach — clear target
        ctx.blackboard.pop("move_target", None)
        return await wander_action(ctx)

    next_x, next_y = path[0]
    direction = direction_to(sx, sy, next_x, next_y)

    import asyncio

    from anima.client.packets import build_walk_request

    # Turn if needed
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

    # Step
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
    """Parse LLM response into an action dict."""
    # Try to extract JSON from the response
    text = text.strip()

    # Try direct JSON parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON in markdown code block
    for start, end in [("```json", "```"), ("```", "```"), ("{", None)]:
        idx = text.find(start)
        if idx == -1:
            continue
        if start == "{":
            # Find matching brace
            brace_start = idx
            depth = 0
            for i in range(brace_start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[brace_start : i + 1])
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
