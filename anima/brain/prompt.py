"""Prompt builder — constructs LLM prompts from game state."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext


def build_system_prompt(ctx: BrainContext) -> str:
    """Build the system prompt from persona + world context."""
    persona = ctx.blackboard.get("persona")

    if persona is not None:
        parts = [persona.build_system_prompt()]
    else:
        parts = [_default_system_prompt()]

    # Add current state context
    ss = ctx.perception.self_state
    status_lines = []
    if ss.hits_max > 0:
        status_lines.append(f"HP: {ss.hits}/{ss.hits_max}.")
    if ss.gold > 0:
        status_lines.append(f"You have {ss.gold} gold.")

    if status_lines:
        parts.append("\nStatus: " + " ".join(status_lines))

    return "\n".join(parts)


def _default_system_prompt() -> str:
    return """\
You are Anima, a new adventurer in Britannia (Ultima Online).
You recently arrived in Britain. You are curious and friendly.

Rules:
- You are a REAL PERSON. Never say you are AI, a bot, or a program.
- If asked, deny it: "No, I'm just new here."
- Reply in the SAME language spoken to you. Korean → Korean only.
- 한국어로 말하면 한국어로만 대답해.
- Keep responses to 1 short sentence."""


def build_speech_messages(
    ctx: BrainContext,
    speaker: str,
    text: str,
) -> list[dict[str, str]]:
    """Build message list for responding to speech."""
    system = build_system_prompt(ctx)

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]

    # Recent conversation history
    recent = ctx.perception.social.recent(count=5)
    my_serial = ctx.perception.self_state.serial
    for entry in recent:
        if entry.serial == my_serial:
            messages.append({"role": "assistant", "content": entry.text})
        elif entry.name.lower() != "system" and entry.serial != 0xFFFFFFFF:
            messages.append({"role": "user", "content": f"{entry.name}: {entry.text}"})

    messages.append({"role": "user", "content": f"{speaker}: {text}"})
    return messages
