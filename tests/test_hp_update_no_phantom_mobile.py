"""0xA1 UpdateCurrentHealth must never CREATE a mobile.

Regression: the non-self branch of handle_hp_update called
world.get_or_create_mobile(serial), so a health update for a serial not (yet /
no longer) in view spawned a brand-new MobileInfo at (0,0). ServUO normalises a
non-self bar (MobileHitsN / AttributeNormalizer) to hits_max=25 with
hits=(cur*25//max), so a foe at <2% HP or one killed between our 0x78 and its
0xA1 arrives as hits_max=25, hits=0 — and the freshly-spawned phantom is
immediately `is_dead` (hits_max>0 and hits<=0): a positionless corpse that
leaks into world.mobiles and pollutes the swarm tally / target / heal scans.

ClassicUO's UpdateHitpoints (PacketHandlers.cs:3402) returns when
`world.Get(serial) == null`; only the spatial packets (0x78/0x77/0x20) create
mobiles. This locks that invariant — matching the existing 0xAF DisplayDeath
"no phantom for a never-seen mobile" guarantee.
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


def _hp_update(serial: int, hits_max: int, hits: int) -> bytes:
    """[0xA1][serial:u32][hits_max:u16][hits:u16] — fixed 9-byte packet."""
    return struct.pack(">BIHH", 0xA1, serial, hits_max, hits)


def test_hp_update_does_not_create_unseen_mobile():
    h, p, _ = _make_stack()
    assert FOE not in p.world.mobiles

    # Normalised "dead" bar (hits_max=25, hits=0) for a serial never in view.
    h.dispatch(0xA1, _hp_update(FOE, 25, 0))

    # No phantom mobile was spawned — so no positionless corpse to leak.
    assert FOE not in p.world.mobiles


def test_hp_update_low_hp_unseen_mobile_creates_no_phantom_corpse():
    """The exact failure mode: a normalised bar that reads as a corpse must not
    materialise an is_dead phantom at (0,0)."""
    h, p, _ = _make_stack()

    h.dispatch(0xA1, _hp_update(FOE, 25, 0))

    assert p.world.mobiles.get(FOE) is None
    # nothing parked at the map origin
    assert p.world.nearby_mobiles(0, 0) == []


def test_hp_update_refreshes_an_already_seen_mobile():
    """When the mobile already exists (entered view via a spatial packet), its
    health bar is still updated — the guard only blocks creation."""
    h, p, _ = _make_stack()
    mob = p.world.get_or_create_mobile(FOE)
    mob.x, mob.y = 100, 200
    mob.hits_max, mob.hits = 25, 25

    h.dispatch(0xA1, _hp_update(FOE, 25, 6))

    refreshed = p.world.mobiles[FOE]
    assert refreshed.hits == 6
    assert refreshed.hits_max == 25
    # position is untouched (0xA1 carries none)
    assert (refreshed.x, refreshed.y) == (100, 200)


def test_hp_update_self_still_updates_self_state():
    """The self branch is unaffected: our own bar carries real (un-normalised)
    values and must still land on self_state."""
    h, p, _ = _make_stack()

    h.dispatch(0xA1, _hp_update(PLAYER, 120, 47))

    assert p.self_state.hits == 47
    assert p.self_state.hits_max == 120
    # and self is never stored as a world mobile
    assert PLAYER not in p.world.mobiles
