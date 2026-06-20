"""0x11 CharacterStatus must persist a learned name to the durable OPL cache.

Regression: the non-self branch of handle_character_status set ``mob.name``
on the live mobile but never wrote it to ``world.opl_names``. That cache is
the ONLY thing that survives a 0x1D Delete — get_or_create_mobile re-seeds a
re-entering mobile's name from it. The 0x1C single-click LABEL and 0xD6
MegaCliloc name sources both call ``remember_opl_name``; the 0x11 status reply
(a primary foe/NPC name source, sent on a single-click / health-bar query) did
not, so a name learned from a status reply was silently lost the moment the
mobile left view and re-entered, breaking the synchronous name-keyed gates
until a fresh OPL round-trip.
"""

import struct

from anima.client.handler import PacketHandler
from anima.perception import Perception
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager

PLAYER = 0x00000001
FOE = 0x40000010


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    p = Perception(player_serial=PLAYER)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _character_status(serial: int, name: str, hits: int, hits_max: int) -> bytes:
    """Variable-length 0x11 CharacterStatus, compact (type 0) form."""
    name_field = name.encode("ascii")[:30].ljust(30, b"\x00")
    body = struct.pack(">I", serial) + name_field + struct.pack(
        ">HHBB", hits, hits_max, 0, 0
    )
    return struct.pack(">BH", 0x11, 3 + len(body)) + body


def test_status_name_survives_despawn_and_reentry():
    h, p, _ = _make_stack()

    # Foe enters view via a spatial packet, then we single-click it: the
    # status reply teaches us its name.
    mob = p.world.get_or_create_mobile(FOE)
    mob.x, mob.y = 100, 200
    mob.hits_max, mob.hits = 25, 25
    h.dispatch(0x11, _character_status(FOE, "An Orc", hits=6, hits_max=25))
    assert p.world.mobiles[FOE].name == "An Orc"

    # The name must be in the durable cache that survives a 0x1D Delete.
    assert p.world.opl_names.get(FOE) == "An Orc"

    # Foe leaves view (0x1D Delete) — the live MobileInfo is dropped, but the
    # cache is intentionally kept.
    h.dispatch(0x1D, struct.pack(">BI", 0x1D, FOE))
    assert FOE not in p.world.mobiles

    # Re-entry recreates a blank MobileInfo, re-seeded from opl_names.
    reborn = p.world.get_or_create_mobile(FOE)
    assert reborn.name == "An Orc"


def test_status_does_not_cache_an_empty_name():
    h, p, _ = _make_stack()
    mob = p.world.get_or_create_mobile(FOE)
    mob.x, mob.y = 100, 200

    # A blank name field must not write an empty string into the cache.
    h.dispatch(0x11, _character_status(FOE, "", hits=6, hits_max=25))
    assert FOE not in p.world.opl_names


def test_status_for_unseen_mobile_caches_no_name():
    """The phantom guard still wins: a status reply for a serial never in view
    creates no mobile AND caches no name (touch_existing_mobile returns None
    before the cache write)."""
    h, p, _ = _make_stack()
    h.dispatch(0x11, _character_status(FOE, "An Orc", hits=0, hits_max=25))
    assert FOE not in p.world.mobiles
    assert FOE not in p.world.opl_names
