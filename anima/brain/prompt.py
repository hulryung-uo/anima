"""Prompt builder — constructs LLM prompts from game state."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

MAX_CONVERSATION_HISTORY = 20


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


def _detect_korean(text: str) -> bool:
    return any("\uac00" <= c <= "\ud7a3" for c in text)


def build_speech_messages(
    ctx: BrainContext,
    speaker: str,
    text: str,
) -> list[dict[str, str]]:
    """Build message list for responding to speech.

    Uses a dedicated conversation history in the blackboard
    so think_speak monologue doesn't pollute the context.
    """
    system = build_system_prompt(ctx)

    # Enforce language matching based on input
    if _detect_korean(text):
        system += (
            "\n\nIMPORTANT: The player is speaking Korean."
            " You MUST reply in Korean only. 반드시 한국어로만 대답하세요."
        )
    else:
        system += "\n\nIMPORTANT: Reply in English only."

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]

    # Get conversation history from blackboard
    history: list[dict[str, str]] = ctx.blackboard.get("conversation_history", [])
    for msg in history:
        messages.append(msg)

    # Add the current speech
    messages.append({"role": "user", "content": f"{speaker}: {text}"})
    return messages


def record_conversation(ctx: BrainContext, role: str, content: str) -> None:
    """Record a message in the conversation history.

    Call this after receiving player speech (role='user')
    and after Anima responds (role='assistant').
    """
    history: list[dict[str, str]] = ctx.blackboard.setdefault(
        "conversation_history", []
    )
    history.append({"role": role, "content": content})

    # Keep history bounded
    if len(history) > MAX_CONVERSATION_HISTORY:
        del history[: len(history) - MAX_CONVERSATION_HISTORY]
