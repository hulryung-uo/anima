"""Self poison flags must reset when our body flips to a ghost (death).

``is_poisoned`` / ``poison_level`` only ever arrive via the 0x17 poison-bar
entry, and ServUO does not reliably re-send a poison-cured 0x17 for our own
character around death/resurrect (it cures poison in Mobile.OnDeath; the body
flip to a ghost is the authoritative death signal). Before this fix a self that
was poisoned at the moment of death stayed is_poisoned==True through the ghost
period and out the other side of a resurrect, so the planner cure-priority gate
and combat_loop poison branch kept firing a phantom cure on a full-HP, un-
poisoned, freshly-resurrected agent. The body flip must clear it, mirroring how
is_yellow_health is already re-derived on every self body update.
"""

import struct

from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager

SELF = 0x00000001
LIVING_BODY = 0x0190
GHOST_BODY = 0x0192


def _make_stack():
    p = Perception(player_serial=SELF)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p


def _poison_0x17(serial: int, level_flag: int) -> bytes:
    """0x17 NewHealthbarUpdate carrying one poison entry (flag = level + 1)."""
    body = bytearray()
    body += struct.pack(">I", serial)
    body += struct.pack(">H", 1)  # count
    body += struct.pack(">H", 1)  # status_type 1 = poison
    body += struct.pack(">B", level_flag)
    pkt = bytearray([0x17])
    pkt += struct.pack(">H", 0)
    pkt += body
    struct.pack_into(">H", pkt, 1, len(pkt))
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


def test_ghost_body_clears_self_poison_via_0x20():
    h, p = _make_stack()

    # Poisoned (level 2 = flag 3) while alive.
    h.dispatch(0x17, _poison_0x17(SELF, 3))
    assert p.self_state.is_poisoned is True
    assert p.self_state.poison_level == 2

    # We die: body flips to a ghost (no paired poison-cured 0x17 arrives).
    h.dispatch(0x20, _mobile_update_0x20(SELF, GHOST_BODY))
    assert p.self_state.is_ghost is True
    assert p.self_state.is_poisoned is False
    assert p.self_state.poison_level == -1


def test_ghost_body_clears_self_poison_via_0x78():
    h, p = _make_stack()

    h.dispatch(0x17, _poison_0x17(SELF, 2))  # level 1
    assert p.self_state.is_poisoned is True

    h.dispatch(0x78, _mobile_incoming_0x78(SELF, GHOST_BODY))
    assert p.self_state.is_ghost is True
    assert p.self_state.is_poisoned is False
    assert p.self_state.poison_level == -1


def test_poison_survives_to_resurrect_without_clear_then_stays_clear():
    """End-to-end: poison -> death (ghost) clears -> resurrect leaves it clear."""
    h, p = _make_stack()

    h.dispatch(0x17, _poison_0x17(SELF, 4))  # lethal (level 3)
    assert p.self_state.is_poisoned is True

    # Death.
    h.dispatch(0x20, _mobile_update_0x20(SELF, GHOST_BODY))
    assert p.self_state.is_poisoned is False

    # Resurrect: body flips back to a living body via a plain 0x20 (no 0x17).
    h.dispatch(0x20, _mobile_update_0x20(SELF, LIVING_BODY))
    assert p.self_state.is_ghost is False
    assert p.self_state.is_poisoned is False
    assert p.self_state.poison_level == -1


def test_living_to_living_body_change_does_not_touch_poison():
    """A non-death body change (e.g. polymorph/mount) must not wipe real poison."""
    h, p = _make_stack()

    h.dispatch(0x20, _mobile_update_0x20(SELF, LIVING_BODY))
    h.dispatch(0x17, _poison_0x17(SELF, 2))  # level 1
    assert p.self_state.is_poisoned is True

    # Some other living body update arrives — poison must persist.
    h.dispatch(0x20, _mobile_update_0x20(SELF, 0x0191))
    assert p.self_state.is_ghost is False
    assert p.self_state.is_poisoned is True
    assert p.self_state.poison_level == 1
