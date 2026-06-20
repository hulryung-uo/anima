"""0x2D MobileAttributes must never CREATE a mobile.

Regression (same class as the 0xA1 fix in 1bf1658): the non-self branch of
handle_mobile_attributes called world.get_or_create_mobile(serial), so a vitals
refresh for a serial not (yet / no longer) in view spawned a brand-new
MobileInfo at (0,0). ServUO normalises a non-self bar (AttributeNormalizer) to
hits_max=25 with hits=(cur*25//max), so a foe at <2% HP — or one
killed/resurrected between our 0x78 and its 0x2D — arrives as hits_max=25,
hits=0, and the freshly-spawned phantom is immediately `is_dead` (hits_max>0 and
hits<=0): a positionless corpse that leaks into world.mobiles and pollutes the
swarm tally / target / heal scans.

ClassicUO's MobileAttributes (PacketHandlers.cs:1765) returns when
`world.Get(serial) == null`; only the spatial packets (0x78/0x77/0x20) create
mobiles. This locks that invariant — matching the 0xA1 UpdateCurrentHealth and
0xAF DisplayDeath "no phantom for a never-seen mobile" guarantees.
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


def _mobile_attributes(
    serial: int,
    hits_max: int,
    hits: int,
    mana_max: int = 0,
    mana: int = 0,
    stam_max: int = 0,
    stam: int = 0,
) -> bytes:
    """[0x2D][serial:u32][hits_max][hits][mana_max][mana][stam_max][stam].

    Fixed 17-byte packet (id + serial:u32 + six u16 vitals fields).
    """
    return struct.pack(
        ">BIHHHHHH", 0x2D, serial, hits_max, hits, mana_max, mana, stam_max, stam
    )


def test_mobile_attributes_does_not_create_unseen_mobile():
    h, p, _ = _make_stack()
    assert FOE not in p.world.mobiles

    # Normalised "dead" bar (hits_max=25, hits=0) for a serial never in view.
    h.dispatch(0x2D, _mobile_attributes(FOE, 25, 0))

    # No phantom mobile was spawned — so no positionless corpse to leak.
    assert FOE not in p.world.mobiles


def test_mobile_attributes_low_hp_unseen_mobile_creates_no_phantom_corpse():
    """The exact failure mode: a normalised bar that reads as a corpse must not
    materialise an is_dead phantom at (0,0)."""
    h, p, _ = _make_stack()

    h.dispatch(0x2D, _mobile_attributes(FOE, 25, 0))

    assert p.world.mobiles.get(FOE) is None
    # nothing parked at the map origin
    assert p.world.nearby_mobiles(0, 0) == []


def test_mobile_attributes_refreshes_an_already_seen_mobile():
    """When the mobile already exists (entered view via a spatial packet), its
    health bar is still updated — the guard only blocks creation."""
    h, p, _ = _make_stack()
    mob = p.world.get_or_create_mobile(FOE)
    mob.x, mob.y = 100, 200
    mob.hits_max, mob.hits = 25, 25

    h.dispatch(0x2D, _mobile_attributes(FOE, 25, 6))

    refreshed = p.world.mobiles[FOE]
    assert refreshed.hits == 6
    assert refreshed.hits_max == 25
    # position is untouched (0x2D carries none)
    assert (refreshed.x, refreshed.y) == (100, 200)


def test_mobile_attributes_self_still_updates_self_state():
    """The self branch is unaffected: our own packet carries real
    (un-normalised) vitals and must still land on self_state."""
    h, p, _ = _make_stack()

    h.dispatch(
        0x2D, _mobile_attributes(PLAYER, 120, 47, mana_max=80, mana=30, stam_max=90, stam=55)
    )

    assert p.self_state.hits == 47
    assert p.self_state.hits_max == 120
    assert p.self_state.mana == 30
    assert p.self_state.stam == 55
    # and self is never stored as a world mobile
    assert PLAYER not in p.world.mobiles
