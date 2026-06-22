"""The cached open-container / context-menu pointer must reset when our body
flips to a ghost (death).

``open_container`` (0x24) and ``context_menu_serial`` (0xBF sub 0x14) are only
cleared by a matching 0x1D Delete for that exact serial. Death closes every
open gump/container in ServUO but does NOT destroy the persistent entity behind
it (the bank box stays live server-side), so no 0x1D arrives and the stale
pointer survives the ghost period and the resurrect. ``_wait_for_bank_box``
then treats the pre-death bank serial as "bank open" and deposits silently fail
against a container the server has already closed. The body flip must clear it,
mirroring the self-poison-on-ghost clear.
"""

import struct

from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager

SELF = 0x00000001
LIVING_BODY = 0x0190
GHOST_BODY = 0x0192
BANK_SERIAL = 0x40000099
BACKPACK = 0x40000001


def _make_stack():
    p = Perception(player_serial=SELF)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p


def _container_display_0x24(serial: int) -> bytes:
    """0x24 ContainerDisplay (fixed 7 bytes): [id][serial:u32][gump:u16]."""
    pkt = bytearray([0x24])
    pkt += struct.pack(">I", serial)
    pkt += struct.pack(">H", 0x003C)  # gump graphic
    return bytes(pkt)


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


def test_ghost_body_clears_open_container_via_0x20():
    h, p = _make_stack()

    # A bank box (or corpse) is opened while alive.
    h.dispatch(0x24, _container_display_0x24(BANK_SERIAL))
    assert p.self_state.open_container == BANK_SERIAL

    # We die: body flips to a ghost. ServUO closes the container UI but sends
    # no 0x1D Delete for the persistent bank box.
    h.dispatch(0x20, _mobile_update_0x20(SELF, GHOST_BODY))
    assert p.self_state.is_ghost is True
    assert p.self_state.open_container == 0


def test_ghost_body_clears_open_container_via_0x78():
    h, p = _make_stack()

    h.dispatch(0x24, _container_display_0x24(BANK_SERIAL))
    assert p.self_state.open_container == BANK_SERIAL
    # Also seed a stale context-menu pointer.
    p.self_state.context_menu_serial = BANK_SERIAL
    p.self_state.context_menu = [object()]  # type: ignore[list-item]

    h.dispatch(0x78, _mobile_incoming_0x78(SELF, GHOST_BODY))
    assert p.self_state.is_ghost is True
    assert p.self_state.open_container == 0
    assert p.self_state.context_menu_serial == 0
    assert p.self_state.context_menu == []


def test_open_container_stays_clear_across_resurrect():
    """End-to-end: open container -> death clears it -> resurrect leaves it clear.

    The old bank serial must NOT re-appear in open_container after resurrect; a
    new bank open arrives on its own fresh 0x24.
    """
    h, p = _make_stack()

    h.dispatch(0x24, _container_display_0x24(BANK_SERIAL))
    assert p.self_state.open_container == BANK_SERIAL

    h.dispatch(0x20, _mobile_update_0x20(SELF, GHOST_BODY))
    assert p.self_state.open_container == 0

    # Resurrect: living body via a plain 0x20 (no 0x24 for the old bank).
    h.dispatch(0x20, _mobile_update_0x20(SELF, LIVING_BODY))
    assert p.self_state.is_ghost is False
    assert p.self_state.open_container == 0


def test_living_to_living_body_change_keeps_open_container():
    """A non-death body change (polymorph/mount) must not wipe a live container."""
    h, p = _make_stack()

    h.dispatch(0x20, _mobile_update_0x20(SELF, LIVING_BODY))
    h.dispatch(0x24, _container_display_0x24(BANK_SERIAL))
    assert p.self_state.open_container == BANK_SERIAL

    # Another living body update arrives — the open container must persist.
    h.dispatch(0x20, _mobile_update_0x20(SELF, 0x0191))
    assert p.self_state.is_ghost is False
    assert p.self_state.open_container == BANK_SERIAL
