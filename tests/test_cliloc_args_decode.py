"""0xC1 / 0xCC cliloc argument decode must match ClassicUO's ReadUnicode{LE,BE}.

Regression: the inline arg decode did ``raw.decode("utf-16-le").rstrip("\\x00")``
over the WHOLE packet remainder. ClassicUO (DisplayClilocString,
PacketHandlers.cs:4768-4779) instead reads ``remains / 2`` UTF-16 code units —
flooring an odd trailing byte and stopping at the first NUL. The codec frames a
variable packet by its declared length, which routinely leaves a 1-byte pad
after the NUL terminator, so the buffer length is odd. ``bytes.decode`` then
raised UnicodeDecodeError, which the handler swallowed into an EMPTY arg string
— rendering the cliloc with blank ~N~ placeholders (combat / loot / skill /
vendor messages arriving as ``[cliloc N]`` or just ``": "``).
"""

import struct

from anima.data import cliloc_text
from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.event_stream import GameEventType
from anima.perception.handlers import _decode_cliloc_args, register_handlers
from anima.perception.walker import WalkerManager

PLAYER = 0x00000001

# A cliloc with a single ~N~ placeholder so the rendered text == the arg.
CLILOC_ONE_ARG = 1042971  # "~1_NOTHING~"
CLILOC_TWO_ARG = 1060658  # "~1_val~: ~2_val~"


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    p = Perception(player_serial=PLAYER)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _c1(cliloc: int, args_blob: bytes) -> bytes:
    """[0xC1][len][serial][graphic][type][hue][font][cliloc][name:30][args]."""
    body = struct.pack(">I", 0)  # serial (0 -> System)
    body += struct.pack(">H", 0)  # graphic
    body += struct.pack(">B", 0)  # msg_type
    body += struct.pack(">H", 0)  # hue
    body += struct.pack(">H", 3)  # font
    body += struct.pack(">I", cliloc)
    body += b"System".ljust(30, b"\x00")
    body += args_blob
    # The handler ignores the length field; the codec has already framed it.
    return struct.pack(">BH", 0xC1, 0) + body


def _last_text(p: Perception) -> str:
    events = [e for e in p.events.poll() if e.type is GameEventType.SPEECH_HEARD]
    assert events, "expected a SPEECH_HEARD event"
    return events[-1].data["text"]


def test_helper_floors_odd_trailing_byte():
    # 'Hi' + 2-byte NUL terminator + ONE odd pad byte -> odd-length buffer.
    blob = "Hi".encode("utf-16-le") + b"\x00\x00" + b"\x00"
    assert len(blob) % 2 == 1
    assert _decode_cliloc_args(blob, big_endian=False) == "Hi"


def test_helper_stops_at_first_nul_not_just_trailing():
    # NUL terminator followed by padding/garbage must be discarded entirely,
    # not merely trimmed when trailing.
    blob = "Hi".encode("utf-16-le") + b"\x00\x00" + "XYZ".encode("utf-16-le")
    assert _decode_cliloc_args(blob, big_endian=False) == "Hi"


def test_helper_big_endian_multi_arg():
    blob = "A\tB".encode("utf-16-be") + b"\x00\x00"
    assert _decode_cliloc_args(blob, big_endian=True) == "A\tB"


def test_c1_odd_length_packet_keeps_args():
    """The core regression: an odd trailing pad byte must NOT nuke the args."""
    assert cliloc_text(CLILOC_ONE_ARG) == "~1_NOTHING~"
    h, p, _ = _make_stack()

    # 'Hello' + NUL terminator + odd pad byte (what a real framed packet leaves).
    args = "Hello".encode("utf-16-le") + b"\x00\x00" + b"\x00"
    h.dispatch(0xC1, _c1(CLILOC_ONE_ARG, args))

    # Before the fix this rendered "" (UnicodeDecodeError -> empty args).
    assert _last_text(p) == "Hello"


def test_c1_even_length_still_works():
    h, p, _ = _make_stack()
    args = "World".encode("utf-16-le") + b"\x00\x00"
    h.dispatch(0xC1, _c1(CLILOC_ONE_ARG, args))
    assert _last_text(p) == "World"


def test_c1_tab_separated_two_args():
    assert cliloc_text(CLILOC_TWO_ARG) == "~1_val~: ~2_val~"
    h, p, _ = _make_stack()
    args = "Damage\t42".encode("utf-16-le") + b"\x00\x00" + b"\x00"
    h.dispatch(0xC1, _c1(CLILOC_TWO_ARG, args))
    assert _last_text(p) == "Damage: 42"


# A real cliloc that references the SAME arg index twice. ClassicUO's
# Translate substitutes every ~N~ by its embedded index, so both copies of
# ~2_DAMAGE~ must be filled.
CLILOC_REPEATED_ARG = 1156056  # "...by ~2_DAMAGE~%. Costs ~2_DAMAGE~%..."


def test_c1_repeated_placeholder_fills_every_occurrence():
    """Regression: count=1 positional substitution dropped the 2nd ~2~.

    Before the fix the second ~2_DAMAGE~ survived the substitution loop and was
    then nuked by the trailing unfilled-placeholder strip, so the rendered text
    read "Costs % of original damage in mana." — the repeated arg vanished.
    """
    base = cliloc_text(CLILOC_REPEATED_ARG)
    # Guard the fixture: the bundled cliloc table must actually carry the
    # repeated placeholder this test exercises.
    assert base.count("~2_DAMAGE~") == 2

    h, p, _ = _make_stack()
    args = "15\t30".encode("utf-16-le") + b"\x00\x00" + b"\x00"
    h.dispatch(0xC1, _c1(CLILOC_REPEATED_ARG, args))

    rendered = _last_text(p)
    assert rendered == (
        "15% chance to reduce incoming damage by 30%. "
        "Costs 30% of original damage in mana."
    )
    # No placeholder text leaked through, and the repeated arg was filled twice.
    assert "~" not in rendered
    assert rendered.count("30%") == 2
