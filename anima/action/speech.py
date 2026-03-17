"""Speech actions: respond to heard speech."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_unicode_speech

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext, Status

logger = structlog.get_logger()

GREETINGS = {"hello", "hi", "hey", "greetings", "hail"}
GREETING_RESPONSES = [
    "Hello there!",
    "Hi! Nice to meet you.",
    "Hey! How are you?",
    "Greetings, friend!",
    "Hail!",
]


async def respond_to_speech(ctx: BrainContext) -> Status:
    """Check blackboard for pending speech and respond.

    Tier 1: Pattern-match greetings for instant response.
    Tier 2: Use LLM for everything else.
    """
    from anima.brain.behavior_tree import Status

    pending = ctx.blackboard.get("pending_speech")
    if not pending:
        return Status.FAILURE

    speech = pending.pop(0)
    if not pending:
        del ctx.blackboard["pending_speech"]

    text = speech.get("text", "").strip()
    speaker = speech.get("name", "someone")
    serial = speech.get("serial", 0)

    # Don't respond to our own speech or system messages
    if serial == ctx.perception.self_state.serial:
        return Status.FAILURE
    if serial == 0xFFFFFFFF or speaker.lower() == "system":
        return Status.FAILURE

    # Tier 1: Pattern-match greetings
    words = set(text.lower().split())
    if words & GREETINGS and len(words) <= 3:
        response = random.choice(GREETING_RESPONSES)
        await ctx.conn.send_packet(build_unicode_speech(response))
        logger.info("speech_t1", to=speaker, text=response)
        return Status.SUCCESS

    # Tier 2: LLM response
    if ctx.llm is not None:
        from anima.brain.prompt import build_speech_messages

        messages = build_speech_messages(ctx, speaker, text)
        result = await ctx.llm.chat(messages)
        if result.text:
            # Truncate to UO speech limit and clean up
            response = result.text[:200]
            await ctx.conn.send_packet(build_unicode_speech(response))
            logger.info(
                "speech_t2",
                to=speaker,
                text=response,
                duration_ms=f"{result.total_duration_ms:.0f}",
            )
            return Status.SUCCESS
        # LLM failed (timeout, error) — fall through to fallback
        logger.warning("speech_llm_failed", to=speaker)

    # Fallback: generic response when no LLM available
    response = f"I heard you, {speaker}."
    await ctx.conn.send_packet(build_unicode_speech(response))
    logger.info("speech_fallback", to=speaker, text=response)
    return Status.SUCCESS
