"""0x77 MobileMoving / 0x78 MobileIncoming decode a mobile's notoriety byte.

Regression: the handlers gated the assignment behind ``1 <= notoriety <= 7``,
silently DROPPING any byte that carried the 0x40 overlay/temporary bit (e.g.
0x46 = MURDERER|0x40). The mobile then kept its stale default INNOCENT, so the
combat / flee / social notoriety gates that read ``mob.notoriety`` saw a red
murderer as a blue innocent. ClassicUO assigns the flag unconditionally and
strips 0x40; we mirror the existing 0x22 ConfirmWalk decode.
"""

import struct

from anima.perception import Perception
from anima.perception.enums import NotorietyFlag
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager
from anima.client.handler import PacketHandler

_SERIAL = 0x00001234


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _mobile_moving(noto: int, serial: int = _SERIAL) -> bytes:
    """[0x77][serial:u32][body:u16][x:u16][y:u16][z:i8][dir:u8][hue:u16][flags:u8][noto:u8]."""
    return struct.pack(
        ">BIHHHbBHBB", 0x77, serial, 0x0190, 100, 100, 0, 1, 0, 0x00, noto
    )


def _mobile_incoming(noto: int, serial: int = _SERIAL) -> bytes:
    """0x78 MobileIncoming (variable): id + len(u16) + body, terminated by serial 0."""
    body = struct.pack(
        ">IHHHbBHBB", serial, 0x0190, 100, 100, 0, 1, 0, 0x00, noto
    )
    body += struct.pack(">I", 0)  # empty equipment list terminator
    length = len(body) + 3
    return struct.pack(">BH", 0x78, length) + body


def test_mobile_moving_decodes_murderer_with_overlay_bit():
    """0x46 = MURDERER(6) | 0x40 must decode to MURDERER, not be dropped."""
    h, p, _ = _make_stack()
    h.dispatch(0x77, _mobile_moving(0x40 | NotorietyFlag.MURDERER.value))
    mob = p.world.mobiles[_SERIAL]
    assert mob.notoriety == NotorietyFlag.MURDERER


def test_mobile_moving_decodes_plain_criminal():
    h, p, _ = _make_stack()
    h.dispatch(0x77, _mobile_moving(NotorietyFlag.CRIMINAL.value))
    assert p.world.mobiles[_SERIAL].notoriety == NotorietyFlag.CRIMINAL


def test_mobile_moving_out_of_range_coerces_to_innocent():
    h, p, _ = _make_stack()
    # Seed a non-default value, then a bogus 0x00 byte must reset to INNOCENT.
    h.dispatch(0x77, _mobile_moving(NotorietyFlag.MURDERER.value))
    assert p.world.mobiles[_SERIAL].notoriety == NotorietyFlag.MURDERER
    h.dispatch(0x77, _mobile_moving(0x00))
    assert p.world.mobiles[_SERIAL].notoriety == NotorietyFlag.INNOCENT


def test_mobile_incoming_decodes_criminal_with_overlay_bit():
    """0x44 = CRIMINAL(4) | 0x40 on a 0x78 must decode to CRIMINAL."""
    h, p, _ = _make_stack()
    h.dispatch(0x78, _mobile_incoming(0x40 | NotorietyFlag.CRIMINAL.value))
    mob = p.world.mobiles[_SERIAL]
    assert mob.notoriety == NotorietyFlag.CRIMINAL


def test_mobile_incoming_decodes_invulnerable_with_overlay_bit():
    """0x47 = INVULNERABLE(7) | 0x40 (yellow vendor) must decode to INVULNERABLE."""
    h, p, _ = _make_stack()
    h.dispatch(0x78, _mobile_incoming(0x40 | NotorietyFlag.INVULNERABLE.value))
    assert p.world.mobiles[_SERIAL].notoriety == NotorietyFlag.INVULNERABLE
