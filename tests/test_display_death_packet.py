"""0xAF DisplayDeath is the server's authoritative "this mobile died" signal.

Regression: the packet was in PACKET_LENGTHS (fixed 13) but had no handler, so
perception only learned a foe died from its last-seen health bar reading
hits<=0 — which AttributeNormalizer truncation and 0xA1 update-cadence gaps make
unreliable. Decoding 0xAF marks the foe dead deterministically (so
MobileInfo.is_dead and the combat_loop kill-confirm arm both fire) and emits a
MOBILE_DIED event carrying the corpse serial for the loot path.
"""

import struct

from anima.perception import Perception
from anima.perception.event_stream import GameEventType
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager
from anima.client.handler import PacketHandler

PLAYER = 0x00000001
FOE = 0x40000010
CORPSE = 0x40006006


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    p = Perception(player_serial=PLAYER)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _display_death(serial: int, corpse: int, running: int = 0) -> bytes:
    """[0xAF][killed:u32][corpse:u32][running:u32] — the fixed 13-byte packet."""
    return struct.pack(">BIII", 0xAF, serial, corpse, running)


def test_display_death_marks_known_foe_dead():
    h, p, _ = _make_stack()
    # A foe whose last health bar read positive (e.g. 3/100 normalised to 0..25
    # could even round UP, or a kill landed between 0xA1 updates).
    foe = p.world.get_or_create_mobile(FOE)
    foe.hits_max = 25
    foe.hits = 1
    assert foe.is_dead is False

    h.dispatch(0xAF, _display_death(FOE, CORPSE))

    assert foe.hits == 0
    assert foe.is_dead is True


def test_display_death_emits_event_with_corpse_serial():
    h, p, _ = _make_stack()
    p.world.get_or_create_mobile(FOE)
    p.events.poll()  # drain the MOBILE_APPEARED-style noise, if any

    h.dispatch(0xAF, _display_death(FOE, CORPSE))

    died = [e for e in p.events.poll() if e.type is GameEventType.MOBILE_DIED]
    assert len(died) == 1
    assert died[0].data["serial"] == FOE
    assert died[0].data["corpse_serial"] == CORPSE


def test_display_death_bumps_unqueried_hits_max_so_is_dead_fires():
    """A foe we never status-queried defaults hits_max=0; that must not block
    the death from registering (is_dead requires hits_max > 0)."""
    h, p, _ = _make_stack()
    foe = p.world.get_or_create_mobile(FOE)
    assert foe.hits_max == 0
    assert foe.is_dead is False  # un-queried mob is NOT a corpse

    h.dispatch(0xAF, _display_death(FOE, CORPSE))

    assert foe.hits == 0
    assert foe.hits_max > 0
    assert foe.is_dead is True


def test_display_death_ignores_own_death():
    """Self-death is driven by the death screen / resurrect flow, not 0xAF."""
    h, p, _ = _make_stack()
    p.self_state.hits = 0  # already dead-ish; handler must not emit a foe death
    p.events.poll()

    h.dispatch(0xAF, _display_death(PLAYER, CORPSE))

    died = [e for e in p.events.poll() if e.type is GameEventType.MOBILE_DIED]
    assert died == []


def test_display_death_ignores_never_seen_mobile():
    """No phantom mobile is created for a serial that was never in view
    (ClassicUO returns early when owner == null)."""
    h, p, _ = _make_stack()
    assert FOE not in p.world.mobiles

    h.dispatch(0xAF, _display_death(FOE, CORPSE))

    assert FOE not in p.world.mobiles
    died = [e for e in p.events.poll() if e.type is GameEventType.MOBILE_DIED]
    assert died == []


def test_display_death_truncated_packet_is_ignored():
    h, p, _ = _make_stack()
    foe = p.world.get_or_create_mobile(FOE)
    foe.hits_max = 25
    foe.hits = 10

    # 9 bytes: id + killed serial + half a corpse serial — short of the fixed 13.
    h.dispatch(0xAF, struct.pack(">BI", 0xAF, FOE) + b"\x00")

    assert foe.hits == 10  # untouched
    assert foe.is_dead is False
