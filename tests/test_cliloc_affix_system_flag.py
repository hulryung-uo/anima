"""0xCC SendLocalizedMessageAffix: the System affix flag routes to SYSTEM.

Regression: the handler set ``msg_type = 6`` (MessageType.LABEL) when the
AffixType.System flag was set, but ClassicUO sets ``type = MessageType.System``
(value 1) there (PacketHandlers.cs DisplayClilocString). LABEL (6) is the
single-click NPC name response — a different semantic. ServUO delivers many
gameplay notices (skill/spell/quest results, the "you must wait" throttle) via
SendLocalizedMessageAffix with the System flag, so the bug mis-tagged every one
of them as a name label in the speech journal and the SPEECH_HEARD event.
"""

import struct

from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.enums import MessageType
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _affix_packet(
    serial: int,
    cliloc: int,
    flags: int,
    name: str,
    affix: str,
    msg_type: int = 0,
) -> bytes:
    """Build a 0xCC SendLocalizedMessageAffix packet (no UTF-16 args).

    Layout: [0xCC][len:u16][serial:u32][graphic:u16][msg_type:u8][hue:u16]
            [font:u16][cliloc:u32][flags:u8][name:ascii30][affix:ascii NUL]
    """
    body = b""
    body += struct.pack(">I", serial)
    body += struct.pack(">H", 0)  # graphic
    body += struct.pack(">B", msg_type)
    body += struct.pack(">H", 0)  # hue
    body += struct.pack(">H", 3)  # font
    body += struct.pack(">I", cliloc)
    body += struct.pack(">B", flags)
    name_bytes = name.encode("ascii")[:30]
    body += name_bytes + b"\x00" * (30 - len(name_bytes))
    body += affix.encode("ascii") + b"\x00"  # NUL-terminated affix
    full = struct.pack(">B", 0xCC) + struct.pack(">H", 0) + body
    # Patch the real length field in.
    return full[:1] + struct.pack(">H", len(full)) + full[3:]


# AffixType flags (ClassicUO MessageManager.AffixType): Prepend=0x01, System=0x02
_PREPEND = 0x01
_SYSTEM = 0x02

# cliloc 3000201 = "You must wait to perform another action." — a no-arg
# ServUO system message commonly delivered via SendLocalizedMessageAffix.
_CLILOC = 3000201


def test_affix_system_flag_is_system_not_label():
    h, p, _ = _make_stack()
    pkt = _affix_packet(0x0000ABCD, _CLILOC, _SYSTEM, "System", "", msg_type=0)
    h.dispatch(0xCC, pkt)

    entry = p.social.journal[-1]
    assert entry.text == "You must wait to perform another action."
    # The System affix must produce a SYSTEM-typed journal entry (matching
    # ClassicUO), never a LABEL (the old `msg_type = 6` bug).
    assert entry.msg_type == MessageType.SYSTEM
    assert entry.msg_type != MessageType.LABEL


def test_affix_without_system_flag_keeps_wire_msg_type():
    """No System flag -> the on-wire msg_type is preserved (here REGULAR)."""
    h, p, _ = _make_stack()
    pkt = _affix_packet(0x0000ABCD, _CLILOC, 0x00, "System", "", msg_type=0)
    h.dispatch(0xCC, pkt)

    entry = p.social.journal[-1]
    assert entry.msg_type == MessageType.REGULAR


def test_affix_prepend_with_system_flag_still_system():
    """Prepend|System: affix is prepended AND the type is forced to SYSTEM."""
    h, p, _ = _make_stack()
    pkt = _affix_packet(
        0x0000ABCD, _CLILOC, _PREPEND | _SYSTEM, "System", "ALERT: ", msg_type=2
    )
    h.dispatch(0xCC, pkt)

    entry = p.social.journal[-1]
    assert entry.text.startswith("ALERT: ")
    assert entry.msg_type == MessageType.SYSTEM
