"""CraftBlacksmith reads its result notice from the NEWEST open gump.

Regression: the batch loop drives MAKE LAST on ``ss.gumps[max(keys)]`` (the
freshest CraftGump the server just re-sent), but ``_extract_gump_notice``
iterated ``ss.gumps.values()`` in arbitrary dict order and returned the first
match. When a stale CraftGump from a prior attempt lingered alongside the
fresh one, the current attempt's outcome was read off the PREVIOUS notice —
e.g. a "You create the item." success misclassified as the prior
"You failed to create …" — corrupting the success/fail tally and the
ingot-consumption accounting that drives the crafting loop.
"""
from __future__ import annotations

from types import SimpleNamespace

from anima.perception.gump import GumpData, GumpText
from anima.procedures.craft_blacksmith import CraftBlacksmith, _classify_craft_text


def _craft_gump(gump_id: int, notice: str) -> GumpData:
    """A minimal CraftGump carrying *notice* at the ServUO notice slot
    (AddHtml/AddHtmlLocalized at x=170, y=295)."""
    g = GumpData(serial=1, gump_id=gump_id, x=0, y=0, layout="", text_lines=[notice])
    g.texts.append(GumpText(x=170, y=295, hue=0, text_id=0))
    return g


def test_notice_read_from_newest_gump_when_stale_lingers():
    stale = _craft_gump(
        100, "You failed to create the item, and some of your materials are lost."
    )
    fresh = _craft_gump(200, "You create the item.")
    # Insertion order puts the stale gump first — the old code returned it.
    ss = SimpleNamespace(gumps={stale.gump_id: stale, fresh.gump_id: fresh})

    notice = CraftBlacksmith._extract_gump_notice(ss)
    assert notice == "You create the item."
    assert _classify_craft_text(notice) == "success"


def test_notice_read_from_newest_gump_regardless_of_insertion_order():
    # Same two gumps, fresh inserted first this time: still the highest
    # gump_id (newest) must win, not insertion order.
    fresh = _craft_gump(200, "You create the item.")
    stale = _craft_gump(
        100, "You failed to create the item, and some of your materials are lost."
    )
    ss = SimpleNamespace(gumps={fresh.gump_id: fresh, stale.gump_id: stale})

    notice = CraftBlacksmith._extract_gump_notice(ss)
    assert notice == "You create the item."


def test_notice_strips_html_basefont_wrapper():
    # String notices arrive wrapped in <BASEFONT COLOR=#......>…</BASEFONT>
    # (ServUO AddHtml path). The wrapper must be stripped.
    g = _craft_gump(
        300,
        "<BASEFONT COLOR=#FFFFFF>You failed to create the item.</BASEFONT>",
    )
    ss = SimpleNamespace(gumps={g.gump_id: g})
    assert CraftBlacksmith._extract_gump_notice(ss) == "You failed to create the item."


def test_no_notice_returns_empty():
    # Header label only (x=10) — must be skipped, no notice.
    g = GumpData(serial=1, gump_id=400, x=0, y=0, layout="", text_lines=["NOTICES"])
    g.texts.append(GumpText(x=10, y=302, hue=0, text_id=0))
    ss = SimpleNamespace(gumps={g.gump_id: g})
    assert CraftBlacksmith._extract_gump_notice(ss) == ""
