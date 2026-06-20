"""CraftTinker recognizes the ServUO "worn out your tool!" message.

Regression: blacksmithy and carpentry both key tool-breakage on cliloc 1044038
"You have worn out your tool!", but tinkering only looked for "broke"/
"destroyed" — neither of which appears in that line. A worn-out tinker tool was
therefore never flagged, so ``execute`` never set ``skill_problem`` to buy a new
one and the agent kept re-opening the crafting gump with a phantom tool, looping
with no output. The fix matches the real token via ``_tool_broke``.
"""

from __future__ import annotations

import time

from unittest.mock import AsyncMock, patch

import pytest

from anima.perception.social_state import MAX_JOURNAL_SIZE, SocialState, SpeechEntry
from anima.skills.crafting.tinker import (
    HATCHET_GRAPHICS,
    INGOT_GRAPHIC,
    TINKER_TOOLS_GRAPHICS,
    CraftTinker,
    _tool_broke,
)

BACKPACK = 0x4001
TINKER_TOOL = next(iter(TINKER_TOOLS_GRAPHICS))
HATCHET = next(iter(HATCHET_GRAPHICS))


def test_tool_broke_matches_worn_out_token():
    # The real ServUO 1044038 line — previously undetected.
    assert _tool_broke(["you have worn out your tool!"]) is True
    # Legacy phrasings still match.
    assert _tool_broke(["your tool broke"]) is True
    assert _tool_broke(["your tool was destroyed"]) is True
    # A clean success line must not be mistaken for breakage.
    assert _tool_broke(["you create the item."]) is False


class _FakeGump:
    """Minimal GumpData stand-in: one matching button, mutable layout."""

    def __init__(self, gump_id: int, layout: str) -> None:
        self.gump_id = gump_id
        self.serial = gump_id
        self.layout = layout

    def find_button_near_text(self, _substring: str):
        from unittest.mock import MagicMock
        return MagicMock(button_id=7)


def _make_ctx(social: SocialState):
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.equipment = {0x15: BACKPACK}
    ss.gumps = {}
    # backpack: a tinker tool, ingots, but NO hatchet (so we craft the hatchet)
    tool = MagicMock(container=BACKPACK, graphic=TINKER_TOOL, serial=0x200)
    ingots = MagicMock(container=BACKPACK, graphic=INGOT_GRAPHIC, amount=20, serial=0x201)
    ctx.perception.world.items = {0x200: tool, 0x201: ingots}
    ctx.perception.social = social
    ctx.conn.send_packet = AsyncMock()
    ctx.blackboard = {}
    return ctx


@pytest.mark.asyncio
async def test_worn_out_tinker_tool_sets_buy_signal():
    """A craft that ends in "worn out your tool!" must report tool breakage and
    push the ``skill_problem`` buy-a-new-tool signal — not be swallowed as a
    plain success/unknown result."""
    skill = CraftTinker()
    social = SocialState()
    ctx = _make_ctx(social)

    gump = _FakeGump(0xABCD, layout="base")
    state = {"sleeps": 0}

    async def fake_sleep(_d):
        state["sleeps"] += 1
        n = state["sleeps"]
        if n == 1:
            # crafting gump appears
            ctx.perception.self_state.gumps[gump.gump_id] = gump
        elif n == 2:
            # gump refreshes after the category click (new layout)
            gump.layout = "category"
        elif n == 3:
            # craft resolves: success line PLUS the worn-out-tool line
            social.add_speech(0x100, "System", "You create the item.", 0)
            social.add_speech(0x100, "System", "You have worn out your tool!", 0)

    with patch(
        "anima.skills.crafting.tinker.asyncio.sleep", new=fake_sleep,
    ):
        result = await skill.execute(ctx)

    assert result.success is True  # the item was still created
    assert "tool broke" in result.message.lower()
    assert "skill_problem" in ctx.blackboard, (
        "worn-out tinker tool must raise the buy-a-new-tool signal"
    )
    assert "tinker tools broke" in ctx.blackboard["skill_problem"].lower()


@pytest.mark.asyncio
async def test_clean_success_does_not_flag_tool_broke():
    """Guard against over-flagging: a plain success with no breakage line must
    NOT set the buy-a-new-tool signal."""
    skill = CraftTinker()
    social = SocialState()
    ctx = _make_ctx(social)

    gump = _FakeGump(0xABCD, layout="base")
    state = {"sleeps": 0}

    async def fake_sleep(_d):
        state["sleeps"] += 1
        n = state["sleeps"]
        if n == 1:
            ctx.perception.self_state.gumps[gump.gump_id] = gump
        elif n == 2:
            gump.layout = "category"
        elif n == 3:
            social.add_speech(0x100, "System", "You create the item.", 0)

    with patch(
        "anima.skills.crafting.tinker.asyncio.sleep", new=fake_sleep,
    ):
        result = await skill.execute(ctx)

    assert result.success is True
    assert "tool broke" not in result.message.lower()
    assert "skill_problem" not in ctx.blackboard


@pytest.mark.asyncio
async def test_craft_success_detected_when_journal_is_full():
    """Regression: the craft-result scan must survive a full (wrapped) journal.

    ``social.journal`` is a ``deque(maxlen=MAX_JOURNAL_SIZE)``. Over a live eval
    it fills well past the cap, so ``len()`` is pinned at the cap and the old
    ``list(journal)[journal_before:]`` slice was ALWAYS empty — a genuine
    "You create the item." success was swallowed as "Crafting result unknown"
    (success=False, reward -1.0), inverting the crafting reward signal. Pre-fill
    the journal to its cap with old-timestamped noise; the success line appended
    during the craft (evicting an old entry) must still be detected.
    """
    skill = CraftTinker()
    social = SocialState()

    # Saturate the deque with stale entries so len(journal) == maxlen and any
    # new append evicts from the left (len stays pinned at the cap).
    old_ts = time.time() - 60.0
    for i in range(MAX_JOURNAL_SIZE):
        social.journal.append(
            SpeechEntry(serial=i, name="noise", text=f"old line {i}",
                        msg_type=0, timestamp=old_ts)
        )
    assert len(social.journal) == MAX_JOURNAL_SIZE

    ctx = _make_ctx(social)
    gump = _FakeGump(0xABCD, layout="base")
    state = {"sleeps": 0}

    async def fake_sleep(_d):
        state["sleeps"] += 1
        n = state["sleeps"]
        if n == 1:
            ctx.perception.self_state.gumps[gump.gump_id] = gump
        elif n == 2:
            gump.layout = "category"
        elif n == 3:
            # Craft resolves: success line lands on an ALREADY-FULL deque.
            social.add_speech(0x100, "System", "You create the item.", 0)

    with patch(
        "anima.skills.crafting.tinker.asyncio.sleep", new=fake_sleep,
    ):
        result = await skill.execute(ctx)

    # The deque is still at the cap (append evicted an old entry), proving the
    # old length-offset slice would have read nothing.
    assert len(social.journal) == MAX_JOURNAL_SIZE
    assert result.success is True, (
        "a real craft success must be detected even when the journal deque is full"
    )
    assert "crafted" in result.message.lower()
