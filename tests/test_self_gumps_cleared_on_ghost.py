"""The cached gump set must reset when our body flips to a ghost (death).

``self_state.gumps`` (populated by 0xB0 OpenGump / 0xDD CompressedGump) is only
removed entry-by-entry by a matching 0xBF CloseGump for that gump_id, or by an
explicit ``ss.gumps.clear()`` in a procedure. ServUO closes every open client
gump in the death sequence but sends NO per-gump 0xBF CloseGump, so a gump open
at the moment of death survives the ghost period and the resurrect. The
craft/bank/tool procedures and actions/gump.py read a non-empty ``ss.gumps`` as
"a gump is already open / the craft window is ready", so a stale pre-death gump
falsely satisfies that gate and the freshly-resurrected agent interacts with a
gump the server already destroyed. The body flip must clear it, mirroring the
self-poison / open-container / vendor-state / pending-target clears.
"""

import struct

from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.gump import GumpData
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager

SELF = 0x00000001
LIVING_BODY = 0x0190
GHOST_BODY = 0x0192
CRAFT_GUMP_ID = 0x38920ABD


def _make_stack():
    p = Perception(player_serial=SELF)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p


def _seed_gump(p, gump_id: int) -> None:
    """Stand in for a 0xB0/0xDD OpenGump landing in the cache while alive."""
    p.self_state.gumps[gump_id] = GumpData(
        serial=SELF, gump_id=gump_id, x=0, y=0, layout="", text_lines=[]
    )


def _mobile_update_0x20(serial: int, body_graphic: int) -> bytes:
    """0x20 MobileUpdate (fixed 19 bytes) carrying a body graphic."""
    pkt = bytearray([0x20])
    pkt += struct.pack(">I", serial)
    pkt += struct.pack(">H", body_graphic)  # body
    pkt += struct.pack(">B", 0)             # graphic_inc
    pkt += struct.pack(">H", 0)             # hue
    pkt += struct.pack(">B", 0)             # flags
    pkt += struct.pack(">H", 100)           # x
    pkt += struct.pack(">H", 200)           # y
    pkt += struct.pack(">H", 0)             # server_id
    pkt += struct.pack(">B", 2)             # direction
    pkt += struct.pack(">b", 0)             # z
    return bytes(pkt)


def _mobile_incoming_0x78(serial: int, body_graphic: int) -> bytes:
    body = bytearray()
    body += struct.pack(">I", serial)
    body += struct.pack(">H", body_graphic)
    body += struct.pack(">H", 100)  # x
    body += struct.pack(">H", 200)  # y
    body += struct.pack(">b", 0)    # z
    body += struct.pack(">B", 2)    # direction
    body += struct.pack(">H", 0)    # hue
    body += struct.pack(">B", 0)    # flags
    body += struct.pack(">B", 1)    # notoriety
    body += struct.pack(">I", 0)    # equipment terminator
    pkt = bytearray([0x78])
    pkt += struct.pack(">H", 0)
    pkt += body
    struct.pack_into(">H", pkt, 1, len(pkt))
    return bytes(pkt)


def test_ghost_body_clears_gumps_via_0x20():
    h, p = _make_stack()

    _seed_gump(p, CRAFT_GUMP_ID)
    assert bool(p.self_state.gumps) is True

    # We die: body flips to a ghost. ServUO closes the gump UI but sends no
    # per-gump 0xBF CloseGump for it.
    h.dispatch(0x20, _mobile_update_0x20(SELF, GHOST_BODY))
    assert p.self_state.is_ghost is True
    assert p.self_state.gumps == {}


def test_ghost_body_clears_gumps_via_0x78():
    h, p = _make_stack()

    _seed_gump(p, CRAFT_GUMP_ID)
    assert bool(p.self_state.gumps) is True

    h.dispatch(0x78, _mobile_incoming_0x78(SELF, GHOST_BODY))
    assert p.self_state.is_ghost is True
    assert p.self_state.gumps == {}


def test_gumps_stay_clear_across_resurrect():
    """Open gump -> death clears it -> resurrect leaves the cache empty.

    The old gump must NOT re-appear after resurrect; a new gump arrives on its
    own fresh 0xB0/0xDD.
    """
    h, p = _make_stack()

    _seed_gump(p, CRAFT_GUMP_ID)
    h.dispatch(0x20, _mobile_update_0x20(SELF, GHOST_BODY))
    assert p.self_state.gumps == {}

    # Resurrect: living body via a plain 0x20 (no gump packet for the old gump).
    h.dispatch(0x20, _mobile_update_0x20(SELF, LIVING_BODY))
    assert p.self_state.is_ghost is False
    assert p.self_state.gumps == {}


def test_living_to_living_body_change_keeps_gumps():
    """A non-death body change (polymorph/mount) must not wipe a live gump."""
    h, p = _make_stack()

    h.dispatch(0x20, _mobile_update_0x20(SELF, LIVING_BODY))
    _seed_gump(p, CRAFT_GUMP_ID)
    assert CRAFT_GUMP_ID in p.self_state.gumps

    # Another living body update arrives — the open gump must persist.
    h.dispatch(0x20, _mobile_update_0x20(SELF, 0x0191))
    assert p.self_state.is_ghost is False
    assert CRAFT_GUMP_ID in p.self_state.gumps
