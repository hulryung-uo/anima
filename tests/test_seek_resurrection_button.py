"""Guard: a gump response must never send a button ServUO would reject.

ServUO's PacketHandlers.DisplayGumpResponse calls state.Dispose() (DISCONNECT)
on any response whose button is neither 0 nor a real gump entry. The young
death gump matched seek_resurrection's res heuristic but has only button 0, so
sending button 1 dropped the connection mid-death-recovery (a 45-min soak ended
at 39s). _safe_button coerces an unknown button to 0 ('close', always valid).
"""
from types import SimpleNamespace

from anima.planner.planner import _SeekResurrection


def _gump(*button_ids):
    return SimpleNamespace(buttons=[SimpleNamespace(button_id=b) for b in button_ids])


def test_unknown_button_coerced_to_close():
    # young-death info gump: only button 0 exists; requesting 1 would disconnect
    assert _SeekResurrection._safe_button(_gump(0), 1) == 0


def test_existing_button_preserved():
    # real ResurrectGump: button 1 exists → keep it (resurrect)
    assert _SeekResurrection._safe_button(_gump(0, 1), 1) == 1


def test_zero_always_safe():
    assert _SeekResurrection._safe_button(_gump(0), 0) == 0
    assert _SeekResurrection._safe_button(_gump(), 0) == 0


def test_no_buttons_coerces_nonzero_to_close():
    assert _SeekResurrection._safe_button(_gump(), 1) == 0
