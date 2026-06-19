"""Speech actions: respond to heard speech."""

from __future__ import annotations

import random
import unicodedata
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_unicode_speech
from anima.memory.rewards import get_reward
from anima.perception.enums import MessageType, NotorietyFlag

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext, Status

logger = structlog.get_logger()

GREETINGS = {"hello", "hi", "hey", "greetings", "hail", "안녕", "반가워", "하이"}


# Notoriety colors that mark the speaker as a foe, not a would-be friend:
# gray criminals, orange enemies, red murderers.
_HOSTILE_NOTORIETY = {NotorietyFlag.CRIMINAL, NotorietyFlag.ENEMY, NotorietyFlag.MURDERER}


# Message types that are NOT a person talking to us and must never draw a
# spoken reply: server SYSTEM lines, single-click LABEL responses ("Hastin the
# baker"), and FOCUS prompts. These ride the SAME 0x1C/0x1D/0xC1/0xCC speech
# packets a real utterance does and surface as SPEECH_HEARD with a mobile-range
# serial, so the bare ``serial >= 0x40000000`` item guard does not catch them.
# think._build_recent_speech already excludes exactly this set from the LLM
# conversation window; the *reply* path must reject it too, or the agent talks
# aloud to a name label / cliloc line — an obvious bot tell.
_NON_CONVERSATIONAL_TYPES = frozenset(
    {MessageType.SYSTEM, MessageType.LABEL, MessageType.FOCUS}
)


def _greeting_tokens(text: str) -> set[str]:
    """Tokenize speech for greeting matching, stripping surrounding punctuation.

    A bare ``text.lower().split()`` leaves punctuation glued to tokens, so the
    most common greeting forms ("Hello!", "안녕!", "hey...", "Hail, friend")
    never match GREETINGS and fall through to the (possibly absent) LLM tier.
    Strip leading/trailing punctuation from each whitespace token so the cheap
    tier-1 fast path still fires.
    """
    tokens: set[str] = set()
    for raw in text.lower().split():
        word = raw.strip("".join(
            c for c in set(raw) if unicodedata.category(c).startswith("P")
        ))
        if word:
            tokens.add(word)
    return tokens


GREETING_RESPONSES = [
    "Hello there!",
    "Hi! Nice to meet you.",
    "Hey! How are you?",
    "Greetings, friend!",
    "Hail!",
]
GREETING_RESPONSES_KR = [
    "안녕!",
    "반가워!",
    "안녕하세요!",
]


async def respond_to_speech(ctx: BrainContext) -> Status:
    """Check blackboard for pending speech and respond.

    Tier 1: Pattern-match greetings for instant response.
    Tier 2: Use LLM for everything else.
    """
    from anima.brain.behavior_tree import Status
    from anima.brain.prompt import build_speech_messages, record_conversation

    pending = ctx.blackboard.get("pending_speech")
    if not pending:
        return Status.FAILURE

    speech = pending.pop(0)
    if not pending:
        del ctx.blackboard["pending_speech"]

    text = speech.get("text", "").strip()
    speaker = speech.get("name", "someone")
    serial = speech.get("serial", 0)
    msg_type = speech.get("type", 0)

    # Don't respond to our own speech, system messages, or NPCs
    if serial == ctx.perception.self_state.serial:
        return Status.FAILURE
    if serial == 0xFFFFFFFF or speaker.lower() == "system":
        return Status.FAILURE
    # In UO the 0x40000000 boundary separates MOBILES (serial < 0x40000000,
    # both players AND NPCs) from ITEMS (serial >= 0x40000000) — it does NOT
    # separate NPCs from players. The previous `serial < 0x40000000` guard was
    # therefore inverted: it dropped EVERY player's speech, leaving the agent
    # mute to all live interaction. Reject only the things that genuinely are
    # not a person talking to us: item-range serials and SYSTEM-type lines
    # (server-emitted text such as cliloc/region messages). REGULAR/EMOTE/
    # WHISPER/YELL from a real mobile fall through and get a reply.
    if serial and serial >= 0x40000000:
        # Item/multi-range serial — not a mobile, can't be a person speaking.
        return Status.FAILURE
    if msg_type in _NON_CONVERSATIONAL_TYPES:
        # SYSTEM / LABEL / FOCUS lines routed through the speech path are not
        # chatter (a single-click name label or a server prompt), even when
        # they carry a mobile-range serial. Mirror _build_recent_speech's
        # filter so we never reply aloud to one.
        return Status.FAILURE

    # Publish to activity feed
    feed = ctx.blackboard.get("activity_feed")
    if feed:
        feed.publish("social", f'{speaker}: "{text[:60]}"', importance=1)

    # Record incoming speech in conversation history
    record_conversation(ctx, "user", f"{speaker}: {text}")

    # Update relationship — someone is talking to us.
    #
    # Disposition/trust are the friend/foe signal the LLM context is built from
    # (memory/retrieval._disposition_word). The relationship table is *only* ever
    # written from this speech path, so the sign chosen here is the agent's whole
    # picture of a person. A flat positive bump on every line meant a red
    # MURDERER or orange ENEMY who simply spammed chat steadily climbed toward
    # "friendly" with rising trust — the exact inversion of reality, and nothing
    # else ever recorded a negative disposition to correct it. Read the speaker's
    # notoriety from the world state: a hostile color earns a negative delta (a
    # foe talking at us makes us warier, not friendlier); everyone else keeps the
    # original small positive bump.
    if ctx.memory_db and serial:
        disp_delta, trust_delta = 0.05, 0.02
        world = getattr(ctx.perception, "world", None)
        mob = world.mobiles.get(serial) if world is not None else None
        if mob is not None and mob.notoriety in _HOSTILE_NOTORIETY:
            disp_delta, trust_delta = -0.05, -0.02
        await ctx.memory_db.update_relationship(
            agent_name=_agent_name(ctx),
            entity_serial=serial,
            entity_name=speaker,
            disposition_delta=disp_delta,
            trust_delta=trust_delta,
            note=f"Spoke to me: {text[:50]}",
        )
        await ctx.memory_db.record_episode(
            agent_name=_agent_name(ctx),
            location_x=ctx.perception.self_state.x,
            location_y=ctx.perception.self_state.y,
            action="speech_received",
            target=speaker,
            outcome="success",
            reward=get_reward("speech_responded"),
            summary=f"{speaker} said: {text[:50]}",
        )

    # Detect language
    is_korean = any("\uac00" <= c <= "\ud7a3" for c in text)

    # Tier 1: Pattern-match greetings
    words = _greeting_tokens(text)
    if words & GREETINGS and len(words) <= 3:
        if is_korean:
            response = random.choice(GREETING_RESPONSES_KR)
        else:
            response = random.choice(GREETING_RESPONSES)
        await ctx.conn.send_packet(build_unicode_speech(response))
        record_conversation(ctx, "assistant", response)
        logger.info("speech_t1", to=speaker, text=response)
        if feed:
            feed.publish("social", f'Replied to {speaker}: "{response}"', importance=2)
        return Status.SUCCESS

    # Tier 2: LLM response
    if ctx.llm is not None:
        from anima.memory.retrieval import retrieve_context
        memory_block = await retrieve_context(ctx)
        messages = build_speech_messages(ctx, speaker, text, memory_block=memory_block)
        result = await ctx.llm.chat(messages)
        if result.text:
            response = result.text[:200]
            await ctx.conn.send_packet(build_unicode_speech(response))
            record_conversation(ctx, "assistant", response)
            logger.info(
                "speech_t2",
                to=speaker,
                text=response,
                duration_ms=f"{result.total_duration_ms:.0f}",
            )
            if feed:
                feed.publish("social", f'Replied to {speaker}: "{response[:60]}"', importance=2)
            return Status.SUCCESS
        logger.warning("speech_llm_failed", to=speaker)

    # Fallback
    #
    # This is the LAST resort: reached when the LLM is absent OR returned no
    # text. It MUST still honour the persona's hard rule "reply in the SAME
    # language" — a Korean player who said something non-greeting would
    # otherwise get an English "I heard you, <name>.", which both breaks
    # immersion and reads exactly like a bot whose model just fell over. The
    # tier-1 greeting path already localizes via GREETING_RESPONSES_KR; mirror
    # that here using the language already detected above so every reply path
    # is language-consistent.
    if is_korean:
        response = "어, 들었어."
    else:
        response = f"I heard you, {speaker}."
    await ctx.conn.send_packet(build_unicode_speech(response))
    record_conversation(ctx, "assistant", response)
    logger.info("speech_fallback", to=speaker, text=response)
    return Status.SUCCESS


def _agent_name(ctx: BrainContext) -> str:
    persona = ctx.blackboard.get("persona")
    return persona.name if persona else "Anima"
