"""Social state: speech journal and nearby player tracking."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from anima.perception.enums import MessageType

MAX_JOURNAL_SIZE = 100


@dataclass
class SpeechEntry:
    serial: int
    name: str
    text: str
    msg_type: MessageType
    hue: int = 0
    timestamp: float = field(default_factory=time.time)


class SocialState:
    """Tracks speech and social interactions."""

    def __init__(self) -> None:
        self.journal: deque[SpeechEntry] = deque(maxlen=MAX_JOURNAL_SIZE)

    def add_speech(
        self,
        serial: int,
        name: str,
        text: str,
        msg_type: int,
        hue: int = 0,
    ) -> SpeechEntry:
        entry = SpeechEntry(
            serial=serial,
            name=name,
            text=text,
            msg_type=(
                MessageType(msg_type)
                if msg_type in MessageType.__members__.values()
                else MessageType.REGULAR
            ),
            hue=hue,
        )
        self.journal.append(entry)
        return entry

    def recent(self, count: int = 10) -> list[SpeechEntry]:
        entries = list(self.journal)
        return entries[-count:]

    def search(self, keyword: str) -> list[SpeechEntry]:
        keyword_lower = keyword.lower()
        return [e for e in self.journal if keyword_lower in e.text.lower()]
