"""Prompt builder — constructs LLM prompts from game state."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

DEFAULT_PERSONA = """\
You are Anima, a new adventurer in Britannia (Ultima Online).
You just arrived in the city of Britain. You are curious, friendly, and a bit naive.
You don't know much about the world yet, but you're eager to learn.
Keep your responses short (1-2 sentences), natural, and in-character.
Never mention that you are an AI. You are a person in this world.
You can speak both English and Korean (한국어). Reply in the same language the other person uses."""


def build_system_prompt(ctx: BrainContext) -> str:
    """Build the system prompt with persona and world context."""
    ss = ctx.perception.self_state
    parts = [DEFAULT_PERSONA]

    # Add current state context
    status_lines = []
    if ss.name:
        status_lines.append(f"Your name is {ss.name}.")
    if ss.hits_max > 0:
        status_lines.append(f"HP: {ss.hits}/{ss.hits_max}.")
    if ss.gold > 0:
        status_lines.append(f"You have {ss.gold} gold.")

    # Nearby entities
    nearby_mobs = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=18)
    if nearby_mobs:
        names = []
        for mob in nearby_mobs[:5]:
            name = mob.name or f"someone (0x{mob.body:04X})"
            names.append(name)
        status_lines.append(f"Nearby people: {', '.join(names)}.")

    if status_lines:
        parts.append("\nCurrent situation:\n" + "\n".join(status_lines))

    return "\n".join(parts)


def build_speech_messages(
    ctx: BrainContext,
    speaker: str,
    text: str,
) -> list[dict[str, str]]:
    """Build message list for responding to speech."""
    system = build_system_prompt(ctx)

    # Include recent conversation history from social state
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]

    recent = ctx.perception.social.recent(count=5)
    my_serial = ctx.perception.self_state.serial
    for entry in recent:
        if entry.serial == my_serial:
            messages.append({"role": "assistant", "content": entry.text})
        elif entry.name.lower() != "system" and entry.serial != 0xFFFFFFFF:
            messages.append({"role": "user", "content": f"{entry.name}: {entry.text}"})

    # The current speech to respond to
    messages.append({"role": "user", "content": f"{speaker}: {text}"})

    return messages
