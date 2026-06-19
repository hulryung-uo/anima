"""Guard: the resurrection gump handler must only answer res-looking gumps.

The old _interact_with_healer poll loop fell back to button-id 1 for *every*
open gump when text-matching failed. A non-resurrection gump that happens to
carry a reply button (a welcome/status window, a vendor prompt, etc.) would
therefore receive a spurious button-1 click, while the real ResurrectGump went
unanswered — leaving the ghost Frozen ("You are frozen and cannot move") until
the poll timed out. _dismiss_pending_res_gumps gates on the res signature
(CONTINUE/OK label or a "resurrect"/"ghost" text line) and is now the single
code path for both repositioning and post-approach polling.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from anima.perception.gump import GumpButton, GumpData, GumpText
from anima.planner.planner import _SeekResurrection


def _reply_button(button_id: int, x: int = 0, y: int = 0) -> GumpButton:
    # button_type 1 == reply (sent to server)
    return GumpButton(x=x, y=y, normal_id=0, pressed_id=0,
                      button_type=1, param=0, button_id=button_id)


def _ctx_with_gumps(gumps: dict[int, GumpData]):
    ss = SimpleNamespace(is_alive=False, gumps=gumps, serial=0x1)
    conn = SimpleNamespace(send_packet=AsyncMock())
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss),
        conn=conn,
        blackboard={},
        bus=None,
    )


@pytest.mark.asyncio
async def test_non_res_gump_is_not_answered():
    # An unrelated gump: a reply button exists, but no CONTINUE/OK label and
    # no res-signature text. It must be left alone (no spurious button click).
    gump = GumpData(
        serial=0xABC, gump_id=0x55, x=0, y=0, layout="",
        text_lines=["Welcome to the shard"],
        buttons=[_reply_button(1, x=10, y=10)],
        texts=[GumpText(x=10, y=10, hue=0, text_id=0)],
    )
    ctx = _ctx_with_gumps({gump.gump_id: gump})
    result = await _SeekResurrection()._dismiss_pending_res_gumps(ctx)
    assert result is False
    ctx.conn.send_packet.assert_not_called()
    # The gump is NOT consumed — a later, correct handler can still act on it.
    assert gump.gump_id in ctx.perception.self_state.gumps


@pytest.mark.asyncio
async def test_res_gump_text_fallback_sends_button_one():
    # A ResurrectGump whose CONTINUE label didn't resolve, but the text body
    # carries the res signature → fall back to button 1 (ServUO CONTINUE).
    gump = GumpData(
        serial=0xABC, gump_id=0x66, x=0, y=0, layout="",
        text_lines=["You are dead. Do you wish to resurrect?"],
        buttons=[_reply_button(1, x=10, y=10)],
        texts=[],  # no resolvable label → find_button_near_text returns None
    )
    ctx = _ctx_with_gumps({gump.gump_id: gump})
    result = await _SeekResurrection()._dismiss_pending_res_gumps(ctx)
    assert result is False  # still a ghost (mock server never revives us)
    ctx.conn.send_packet.assert_called_once()
    assert gump.gump_id not in ctx.perception.self_state.gumps  # consumed


@pytest.mark.asyncio
async def test_res_gump_with_continue_label_uses_that_button():
    # CONTINUE label resolves and sits next to a reply button (id 7) → use 7.
    gump = GumpData(
        serial=0xABC, gump_id=0x77, x=0, y=0, layout="",
        text_lines=["CONTINUE"],
        buttons=[_reply_button(7, x=10, y=10)],
        texts=[GumpText(x=10, y=10, hue=0, text_id=0)],
    )
    ctx = _ctx_with_gumps({gump.gump_id: gump})
    result = await _SeekResurrection()._dismiss_pending_res_gumps(ctx)
    assert result is False
    ctx.conn.send_packet.assert_called_once()
    assert gump.gump_id not in ctx.perception.self_state.gumps
