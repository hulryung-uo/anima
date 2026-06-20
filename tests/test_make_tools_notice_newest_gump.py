"""make_tools reads its craft notice from the NEWEST open gump.

Regression: the inline notice scan in MakeTools.execute iterated
``ss.gumps.values()`` in arbitrary dict order and returned the first text at
``t.y == 295``. make_tools re-suggests itself and runs back-to-back, so a stale
CraftGump from a prior attempt lingers next to the fresh one the server just
re-sent. Reading the stale gump's notice (or a missing notice on a prior
success) masked the real "required skill" reply, so a low-Tinkering smith never
tripped the give-up / buy-instead breaker and looped forever burning ingots.
The scan also matched y exactly and never stripped HTML, so a notice rendered a
few pixels off (or wrapped in <BASEFONT>) was missed entirely.
"""
from __future__ import annotations

from types import SimpleNamespace

from anima.perception.gump import GumpData, GumpText
from anima.procedures.make_tools import _extract_gump_notice


def _craft_gump(gump_id: int, notice: str) -> GumpData:
    """A minimal tinkering CraftGump carrying *notice* at the ServUO notice
    slot (AddHtmlLocalized at x=170, y=295)."""
    g = GumpData(serial=1, gump_id=gump_id, x=0, y=0, layout="", text_lines=[notice])
    g.texts.append(GumpText(x=170, y=295, hue=0, text_id=0))
    return g


def test_notice_read_from_newest_gump_when_stale_lingers():
    # Stale prior-attempt gump has NO notice (a prior success); the fresh gump
    # carries the real "required skill" reply. The old first-match scan returned
    # the empty stale gump and the skill-too-low branch never fired.
    stale = GumpData(serial=1, gump_id=100, x=0, y=0, layout="", text_lines=[])
    fresh = _craft_gump(200, "You don't have the required skill to make that.")
    # Insertion order puts the stale gump first — the old code scanned it first.
    ss = SimpleNamespace(gumps={stale.gump_id: stale, fresh.gump_id: fresh})

    notice = _extract_gump_notice(ss)
    assert notice == "You don't have the required skill to make that."
    assert "required skill" in notice.lower()


def test_newest_wins_regardless_of_insertion_order():
    # Two notice-bearing gumps; the highest gump_id (newest) must win, not
    # insertion order. The stale notice would mis-classify this attempt.
    fresh = _craft_gump(200, "You don't have the required skill to make that.")
    stale = _craft_gump(100, "You create the item and place it in your backpack.")
    ss = SimpleNamespace(gumps={fresh.gump_id: fresh, stale.gump_id: stale})

    assert _extract_gump_notice(ss) == "You don't have the required skill to make that."


def test_notice_strips_html_basefont_wrapper():
    # String notices arrive wrapped in <BASEFONT COLOR=#......>…</BASEFONT>;
    # the exact-y / no-strip scan returned the tag soup (or missed it).
    g = _craft_gump(
        300,
        "<BASEFONT COLOR=#FFFFFF>You don't have enough materials.</BASEFONT>",
    )
    ss = SimpleNamespace(gumps={g.gump_id: g})
    assert _extract_gump_notice(ss) == "You don't have enough materials."


def test_notice_found_when_y_drifts_off_295():
    # The notice can render a few pixels off 295; the old `t.y == 295` exact
    # match missed it. The y-band must still catch it.
    g = GumpData(serial=1, gump_id=500, x=0, y=0, layout="", text_lines=["You lack the required skill."])
    g.texts.append(GumpText(x=170, y=297, hue=0, text_id=0))
    ss = SimpleNamespace(gumps={g.gump_id: g})
    assert _extract_gump_notice(ss) == "You lack the required skill."


def test_header_label_is_not_a_notice():
    # The "NOTICES" header at x=10 must be skipped (x-gate), not returned.
    g = GumpData(serial=1, gump_id=600, x=0, y=0, layout="", text_lines=["NOTICES"])
    g.texts.append(GumpText(x=10, y=302, hue=0, text_id=0))
    ss = SimpleNamespace(gumps={g.gump_id: g})
    assert _extract_gump_notice(ss) == ""


def test_no_gumps_returns_empty():
    ss = SimpleNamespace(gumps={})
    assert _extract_gump_notice(ss) == ""
