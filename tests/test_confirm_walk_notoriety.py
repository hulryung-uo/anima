"""0x22 ConfirmWalk carries the player's own notoriety byte after the seq.

Regression: the handler read only the sequence byte and dropped the trailing
notoriety, so self_state.notoriety stayed pinned at its INNOCENT default and
the agent could never observe it had gone criminal/gray/murderer.
"""

import struct

from anima.perception import Perception
from anima.perception.enums import NotorietyFlag
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager
from anima.client.handler import PacketHandler


def _make_stack() -> tuple[PacketHandler, Perception, WalkerManager]:
    p = Perception(player_serial=0x00000001)
    w = WalkerManager(p.self_state, p.events)
    h = PacketHandler()
    register_handlers(h, p, w)
    return h, p, w


def _confirm_walk(seq: int, noto: int) -> bytes:
    """[0x22][seq:u8][notoriety:u8] — the fixed 3-byte ConfirmWalk."""
    return struct.pack(">BBB", 0x22, seq, noto)


def test_confirm_walk_decodes_criminal_notoriety():
    h, p, w = _make_stack()
    # Default before any step.
    assert p.self_state.notoriety == NotorietyFlag.INNOCENT
    # Drive a pending step so the seq is one the walker accepts.
    seq = w.next_seq if hasattr(w, "next_seq") else 1
    h.dispatch(0x22, _confirm_walk(seq, NotorietyFlag.CRIMINAL.value))
    assert p.self_state.notoriety == NotorietyFlag.CRIMINAL


def test_confirm_walk_decodes_murderer_notoriety():
    h, p, _ = _make_stack()
    h.dispatch(0x22, _confirm_walk(1, NotorietyFlag.MURDERER.value))
    assert p.self_state.notoriety == NotorietyFlag.MURDERER


def test_confirm_walk_masks_temp_bit():
    """ClassicUO strips 0x40 off the notoriety byte before interpreting it."""
    h, p, _ = _make_stack()
    # 0x40 | INNOCENT(1) must resolve back to INNOCENT, not a bogus enum.
    h.dispatch(0x22, _confirm_walk(1, 0x40 | NotorietyFlag.INNOCENT.value))
    assert p.self_state.notoriety == NotorietyFlag.INNOCENT


def test_confirm_walk_out_of_range_coerces_to_innocent():
    """A 0 or >=8 notoriety byte coerces to INNOCENT, matching ClassicUO."""
    h, p, _ = _make_stack()
    p.self_state.notoriety = NotorietyFlag.MURDERER
    h.dispatch(0x22, _confirm_walk(1, 0x00))
    assert p.self_state.notoriety == NotorietyFlag.INNOCENT
    p.self_state.notoriety = NotorietyFlag.MURDERER
    h.dispatch(0x22, _confirm_walk(1, 0x09))
    assert p.self_state.notoriety == NotorietyFlag.INNOCENT
