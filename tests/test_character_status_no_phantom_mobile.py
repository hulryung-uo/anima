"""0x11 CharacterStatus must never CREATE a mobile.

Regression: the non-self branch of handle_character_status called
world.get_or_create_mobile(serial), so a status reply for a serial not (yet /
no longer) in view spawned a brand-new MobileInfo at (0,0). ServUO emits 0x11
in response to a status request (single-click / health-bar query) and its
AttributeNormalizer pins a non-self bar to hits_max=25 with hits=(cur*25//max),
so a reply for a foe at <2% HP — or one killed between our 0x78 and this 0x11 —
arrives as hits=0, hits_max=25, and the freshly-spawned phantom is immediately
`is_dead` (hits_max>0 and hits<=0): a positionless corpse that leaks into
world.mobiles and pollutes the swarm tally / target / heal scans.

ClassicUO's CharacterStatus (PacketHandlers.cs:483) reads the serial, fetches
`world.Get(serial)`, and returns when it is null; only the spatial packets
(0x78/0x77/0x20) create mobiles. This locks that invariant — matching the
0xA1 UpdateCurrentHealth and 0x2D MobileAttributes guards already in place.
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
    """Variable-length 0x11 CharacterStatus, compact (type 0) form.

    [0x11][len:u16][serial:u32][name:30][hits:u16][hits_max:u16]
    [name_change_flag:u8][type:u8]
    """
    name_field = name.encode("ascii")[:30].ljust(30, b"\x00")
    body = struct.pack(">I", serial) + name_field + struct.pack(
        ">HHBB", hits, hits_max, 0, 0
    )
    return struct.pack(">BH", 0x11, 3 + len(body)) + body


def test_character_status_does_not_create_unseen_mobile():
    h, p, _ = _make_stack()
    assert FOE not in p.world.mobiles

    # Normalised "dead" bar (hits=0, hits_max=25) for a serial never in view.
    h.dispatch(0x11, _character_status(FOE, "An Orc", hits=0, hits_max=25))

    # No phantom mobile was spawned — so no positionless corpse to leak.
    assert FOE not in p.world.mobiles
    assert p.world.nearby_mobiles(0, 0) == []


def test_character_status_refreshes_an_already_seen_mobile():
    """When the mobile already exists (entered view via a spatial packet), its
    name/health are still updated — the guard only blocks creation."""
    h, p, _ = _make_stack()
    mob = p.world.get_or_create_mobile(FOE)
    mob.x, mob.y = 100, 200
    mob.hits_max, mob.hits = 25, 25

    h.dispatch(0x11, _character_status(FOE, "An Orc", hits=6, hits_max=25))

    refreshed = p.world.mobiles[FOE]
    assert refreshed.name == "An Orc"
    assert refreshed.hits == 6
    assert refreshed.hits_max == 25
    # position is untouched (0x11 carries none)
    assert (refreshed.x, refreshed.y) == (100, 200)


def test_character_status_self_still_updates_self_state():
    """The self branch is unaffected: our own full status still lands on
    self_state and self is never stored as a world mobile."""
    h, p, _ = _make_stack()

    h.dispatch(0x11, _character_status(PLAYER, "Anima", hits=47, hits_max=120))

    assert p.self_state.name == "Anima"
    assert p.self_state.hits == 47
    assert p.self_state.hits_max == 120
    assert PLAYER not in p.world.mobiles
