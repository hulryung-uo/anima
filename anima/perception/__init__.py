"""Perception facade — combines all world state sub-systems."""

from __future__ import annotations

from anima.perception.event_stream import EventStream, GameEvent, GameEventType
from anima.perception.self_state import SelfState
from anima.perception.social_state import SocialState
from anima.perception.world_state import WorldState


class Perception:
    """Single entry point for all world awareness.

    Brain reads from this; packet handlers write to this.
    """

    def __init__(self, player_serial: int = 0) -> None:
        self.world = WorldState()
        self.self_state = SelfState(serial=player_serial)
        self.social = SocialState()
        self.events = EventStream()

    def poll_events(self) -> list[GameEvent]:
        return self.events.poll()

    def emit(self, event_type: GameEventType, data: dict | None = None) -> None:
        self.events.emit(event_type, data)
