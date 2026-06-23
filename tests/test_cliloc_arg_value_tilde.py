"""A cliloc arg VALUE that contains a ``~N~``-shaped token must survive.

Regression: ``_resolve_cliloc_text`` substituted the args into the base text
and then ran a SECOND ``re.sub(r"~\\d+[^~]*~", "", text)`` over the result to
strip unfilled placeholders. That second pass re-scanned the substituted text,
so any argument value that legitimately carried a ``~N~``-shaped token (a
nested / server-formatted string, an item or runebook description forwarded by
the shard, etc.) was deleted — corrupting the rendered combat / loot / vendor
line. The fix folds substitution and cleanup into one pass over the base text
(out-of-range index -> ""), matching ClassicUO's Translate, which never
re-examines a substituted arg value.
"""

import struct

from anima.client.handler import PacketHandler
from anima.data import cliloc_text
from anima.perception import Perception
from anima.perception.event_stream import GameEventType
from anima.perception.handlers import _resolve_cliloc_text, register_handlers
from anima.perception.walker import WalkerManager

PLAYER = 0x00000001
CLILOC_ONE_ARG = 1042971  # "~1_NOTHING~" -> rendered text == the arg verbatim
CLILOC_TWO_ARG = 1060658  # "~1_val~: ~2_val~"


def test_arg_value_with_tilde_token_is_preserved():
    """The whole rendered text is exactly the arg, tilde token included."""
    assert cliloc_text(CLILOC_ONE_ARG) == "~1_NOTHING~"
    # An arg value that itself contains a ~2~-shaped token. Before the fix the
    # trailing cleanup pass deleted "~2~", leaving "see  goblins".
    assert _resolve_cliloc_text(CLILOC_ONE_ARG, "see ~2~ goblins") == "see ~2~ goblins"


def test_embedded_tilde_token_survives_inside_a_larger_template():
    # base "~1_val~: ~2_val~", arg 1 carries a tilde token, arg 2 is plain.
    out = _resolve_cliloc_text(CLILOC_TWO_ARG, "loot ~3~ here\t42")
    assert out == "loot ~3~ here: 42"


def test_unfilled_placeholder_in_base_is_still_stripped():
    # arg index 2 is missing -> the ~2_val~ placeholder is dropped, not leaked.
    assert _resolve_cliloc_text(CLILOC_TWO_ARG, "only-one") == "only-one:"


def test_no_args_still_strips_base_placeholders():
    assert _resolve_cliloc_text(CLILOC_ONE_ARG, "") == ""


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    p = Perception(player_serial=PLAYER)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _c1(cliloc: int, args_blob: bytes) -> bytes:
    body = struct.pack(">I", 0)  # serial (0 -> System)
    body += struct.pack(">H", 0)  # graphic
    body += struct.pack(">B", 0)  # msg_type
    body += struct.pack(">H", 0)  # hue
    body += struct.pack(">H", 3)  # font
    body += struct.pack(">I", cliloc)
    body += b"System".ljust(30, b"\x00")
    body += args_blob
    return struct.pack(">BH", 0xC1, 0) + body


def test_c1_end_to_end_keeps_arg_tilde_token():
    """End-to-end through the 0xC1 handler, not just the helper."""
    h, p, _ = _make_stack()
    args = "see ~2~ goblins".encode("utf-16-le") + b"\x00\x00" + b"\x00"
    h.dispatch(0xC1, _c1(CLILOC_ONE_ARG, args))
    events = [e for e in p.events.poll() if e.type is GameEventType.SPEECH_HEARD]
    assert events, "expected a SPEECH_HEARD event"
    assert events[-1].data["text"] == "see ~2~ goblins"
