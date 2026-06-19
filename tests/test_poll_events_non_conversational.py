"""Regression: non-conversational SPEECH_HEARD events must not freeze thinking.

Single-click name labels (msg_type 6 = LABEL, e.g. "Hastin the baker"),
FOCUS prompts (msg_type 7) and SYSTEM lines ride the same speech packets a real
utterance does and surface as SPEECH_HEARD events carrying a MOBILE-range serial
(< 0x40000000). ``Brain._poll_events`` used to queue EVERY such event into
``pending_speech`` AND stamp ``last_player_speech = now`` unconditionally.

That second effect is the damaging one: ``llm_think`` treats
``now - last_player_speech < CONVERSATION_TIMEOUT`` as "in conversation" and
suppresses fresh strategising for the whole window. So a passing NPC's name tag
or a server FOCUS prompt — neither of which is a person talking to us — froze
the agent's goal-picking for 10s at a time. ``_build_recent_speech`` and
``respond_to_speech`` both already exclude this exact message-type set; the poll
path must too, so these lines never queue and never stamp the conversation clock.
"""

from __future__ import annotations

from types import SimpleNamespace

from anima.brain.brain import Brain
from anima.perception.enums import MessageType
from anima.perception.event_stream import GameEvent, GameEventType

MY_SERIAL = 0x00000001
MOBILE_SERIAL = 0x00012345  # < 0x40000000: an NPC the label is attributed to


def _make_brain(events: list[GameEvent]) -> Brain:
    self_state = SimpleNamespace(serial=MY_SERIAL)
    perception = SimpleNamespace(
        self_state=self_state,
        poll_events=lambda: events,
    )
    ctx = SimpleNamespace(perception=perception, blackboard={})
    # root tick is never invoked; we drive _poll_events directly.
    return Brain(ctx, root=SimpleNamespace())


def _speech_event(msg_type: int, *, serial: int = MOBILE_SERIAL,
                  name: str = "Hastin", text: str = "Hastin the baker") -> GameEvent:
    return GameEvent(
        type=GameEventType.SPEECH_HEARD,
        data={"serial": serial, "name": name, "text": text, "type": msg_type},
    )


def test_label_event_does_not_queue_or_stamp_conversation() -> None:
    brain = _make_brain([_speech_event(MessageType.LABEL)])
    brain._poll_events()
    bb = brain.context.blackboard
    assert not bb.get("pending_speech"), "label lines must not queue as pending speech"
    assert "last_player_speech" not in bb, "a name label must not start a conversation"


def test_focus_event_does_not_stamp_conversation() -> None:
    brain = _make_brain([_speech_event(MessageType.FOCUS)])
    brain._poll_events()
    assert "last_player_speech" not in brain.context.blackboard


def test_system_typed_event_does_not_stamp_conversation() -> None:
    # SYSTEM-typed line that did NOT carry name="system" (e.g. a region banner
    # attributed to a mobile-range serial) must still be filtered by type.
    brain = _make_brain([_speech_event(MessageType.SYSTEM, name="Britain")])
    brain._poll_events()
    assert "last_player_speech" not in brain.context.blackboard


def test_regular_speech_still_queues_and_stamps() -> None:
    # A real player utterance MUST still queue and start the conversation clock —
    # the filter must not over-reach and mute live interaction.
    brain = _make_brain([_speech_event(MessageType.REGULAR, text="hello there")])
    brain._poll_events()
    bb = brain.context.blackboard
    assert bb.get("pending_speech"), "real speech must queue for a reply"
    assert isinstance(bb.get("last_player_speech"), float)


def test_emote_and_whisper_still_count_as_conversation() -> None:
    for mt in (MessageType.EMOTE, MessageType.WHISPER, MessageType.YELL):
        brain = _make_brain([_speech_event(mt, text="psst")])
        brain._poll_events()
        bb = brain.context.blackboard
        assert bb.get("pending_speech"), f"{mt.name} is real speech and must queue"
        assert isinstance(bb.get("last_player_speech"), float)
