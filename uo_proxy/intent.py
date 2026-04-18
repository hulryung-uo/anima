"""In-game chat → intent label extraction.

When the player types a chat line that starts with a configured prefix
(default `//`), the proxy treats the rest of the line as an intent label,
records it to a separate JSONL stream, and **drops** the packet so it is
never forwarded to the UO server (other players don't see it).

Supported client→server speech packets:
  0xAD Unicode speech (current retail/ClassicUO default)
  0x03 ASCII speech   (legacy — still parsed for completeness)

0xAD wire format (from ClassicUO / anima.client.packets.build_unicode_speech):
  [ID=0xAD][len:u16 BE][type:u8][hue:u16 BE][font:u16 BE][lang:4 ASCII]
  if (type & 0xC0) keyword flag: [keyword_bytes ...][text:utf-8][\x00]
  else:                          [text:utf-16 BE][\x00\x00]

Drop semantics: we never send the packet to the server, so there is also
no server→client echo. On the player's screen nothing appears — that's
fine, the chat line was a private label.
"""

from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_PREFIX",
    "IntentEvent",
    "IntentLogger",
    "extract_intent_from_speech",
]

DEFAULT_PREFIX = "//"


@dataclass
class IntentEvent:
    ts: float
    session_id: str
    label: str
    source: str  # "chat" | "hotkey" | "manual"


def _decode_text_from_ad(packet: bytes) -> str | None:
    """Return the text payload from a 0xAD packet, or None if unparseable.

    Handles both keyword-encoded (UTF-8) and plain (UTF-16 BE) modes.
    """
    if len(packet) < 12 or packet[0] != 0xAD:
        return None
    # header: id(1) len(2) type(1) hue(2) font(2) lang(4) = 12 bytes
    msg_type = packet[3]
    body = packet[12:]

    if msg_type & 0xC0:
        # Keyword mode: keyword_count:u12 + keyword_ids then UTF-8 text + \x00.
        # Keyword block is variable; skip it conservatively by scanning for the
        # first 0x00 byte that *follows* a plausible ASCII/UTF-8 text region.
        # Simpler and good enough for intent labels: everything from the last
        # 0x00-terminator boundary back to where printable bytes start.
        # Implementation: find the terminator, take bytes before it that
        # decode as UTF-8, strip leading non-printable bytes.
        null_pos = body.rfind(b"\x00")
        raw = body[:null_pos] if null_pos >= 0 else body
        # Strip leading bytes until we hit printable ASCII or valid UTF-8 start.
        for i in range(len(raw)):
            if raw[i] >= 0x20:
                try:
                    return raw[i:].decode("utf-8", errors="replace")
                except Exception:
                    return None
        return ""

    # Plain UTF-16 BE, null-terminated (0x0000)
    if len(body) < 2:
        return ""
    # Trim trailing 0x0000 if present
    if body.endswith(b"\x00\x00"):
        body = body[:-2]
    try:
        return body.decode("utf-16-be", errors="replace")
    except Exception:
        return None


def _decode_text_from_03(packet: bytes) -> str | None:
    """Return text from a 0x03 ASCII speech packet, or None."""
    if len(packet) < 8 or packet[0] != 0x03:
        return None
    # id(1) len(2) type(1) hue(2) font(2) = 8 bytes header, then null-terminated ASCII
    body = packet[8:]
    text_end = body.find(b"\x00")
    if text_end >= 0:
        body = body[:text_end]
    try:
        return body.decode("ascii", errors="replace")
    except Exception:
        return None


def extract_intent_from_speech(
    packet: bytes, prefix: str = DEFAULT_PREFIX
) -> tuple[str | None, bool]:
    """Inspect a client→server packet.

    Returns (intent_label, should_drop):
      - (None, False)  → not a speech packet, or not our prefix. Forward unchanged.
      - (label, True)  → prefix hit. Caller should log `label` and NOT forward
                         the packet.
    """
    if not packet:
        return None, False
    pid = packet[0]
    text: str | None
    if pid == 0xAD:
        text = _decode_text_from_ad(packet)
    elif pid == 0x03:
        text = _decode_text_from_03(packet)
    else:
        return None, False

    if text is None:
        return None, False
    text = text.strip()
    if not text.startswith(prefix):
        return None, False
    label = text[len(prefix) :].strip()
    if not label:
        # Empty prefix-only line — still drop it so it stays private,
        # but no intent label to log.
        return None, True
    return label, True


class IntentLogger:
    """Simple synchronous JSONL writer for intent events.

    Unlike ProxyLogger we don't bother with a queue — intent events are
    infrequent (one per user chat line) and the write path is fast.
    """

    SCHEMA = "uo_proxy.intent.v1"

    def __init__(self, out_path: Path) -> None:
        self._out_path = Path(out_path)

    def record(self, event: IntentEvent) -> None:
        try:
            self._out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._out_path, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "schema": self.SCHEMA,
                            "ts": event.ts,
                            "session_id": event.session_id,
                            "label": event.label,
                            "source": event.source,
                        },
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            pass  # never raise into wire path


# Utility used by tests / CLI tools.

def build_plain_unicode_speech(text: str, msg_type: int = 0) -> bytes:
    """Build a minimal 0xAD packet in plain (non-keyword) mode. Test helper."""
    body = bytearray()
    body.append(msg_type)
    body += struct.pack(">H", 0)  # hue
    body += struct.pack(">H", 3)  # font
    body += b"ENU\x00"
    body += text.encode("utf-16-be") + b"\x00\x00"
    total = 3 + len(body)  # id + len + body
    return bytes([0xAD]) + struct.pack(">H", total) + bytes(body)


def now_ts() -> float:
    return time.time()
