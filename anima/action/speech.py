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
    """Check blackboard for pending speech and respond."""
    from anima.brain.behavior_tree import Status

    pending = ctx.blackboard.get("pending_speech")
    if not pending:
        return Status.FAILURE

    speech = pending.pop(0)
    if not pending:
        del ctx.blackboard["pending_speech"]

    text = speech.get("text", "").strip().lower()
    speaker = speech.get("name", "someone")
    serial = speech.get("serial", 0)

    # Don't respond to our own speech
    if serial == ctx.perception.self_state.serial:
        return Status.FAILURE

    # Choose response based on content
    words = set(text.split())
    if words & GREETINGS:
        response = random.choice(GREETING_RESPONSES)
    else:
        response = f"I heard you, {speaker}."

    await ctx.conn.send_packet(build_unicode_speech(response))
    logger.info("speech_response", to=speaker, text=response)
    return Status.SUCCESS
