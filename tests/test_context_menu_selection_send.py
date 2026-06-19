"""select_context_menu_entry must actually SEND the 0xBF/0x15 selection packet.

Regression: the action imported ``build_context_menu_response`` from
``anima.client.packets``, but that name does not exist — the only definition is
``build_context_menu_selection`` (0xBF subcommand 0x15, matching ClassicUO
``Send_PopupMenuSelection``). The bad name lived in a function-local import, so
it never failed at module load: every real call to ``select_context_menu_entry``
raised ``ImportError`` *before* ``send_packet`` ran, so the agent silently never
sent the context-menu selection (e.g. choosing a vendor's "Buy"/"Sell" entry).

This test drives the action with a fake connection and asserts the exact wire
bytes of the selection packet are sent.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from anima.actions import vendor
from anima.client.packets import build_context_menu_selection


@pytest.mark.asyncio
async def test_select_context_menu_entry_sends_selection_packet():
    ctx = MagicMock()
    ctx.conn.send_packet = AsyncMock()

    serial = 0x12345678
    index = 3

    res = await vendor.select_context_menu_entry(ctx, serial, index)

    assert res.success
    ctx.conn.send_packet.assert_awaited_once()
    sent = ctx.conn.send_packet.await_args.args[0]

    # [0xBF][len:u16][0x0015][serial:u32][index:u16] = 11 bytes.
    expected = build_context_menu_selection(serial, index)
    assert sent == expected
    assert sent[0] == 0xBF
    assert sent[3:5] == b"\x00\x15"  # subcommand: PopupMenuSelection
    assert sent[5:9] == serial.to_bytes(4, "big")
    assert sent[9:11] == index.to_bytes(2, "big")
    assert len(sent) == 11
    # Declared length field matches the actual frame length.
    assert int.from_bytes(sent[1:3], "big") == len(sent)


def test_context_menu_response_builder_name_does_not_exist():
    """Guard the exact import that was broken: the builder is *_selection."""
    import anima.client.packets as packets

    assert hasattr(packets, "build_context_menu_selection")
    assert not hasattr(packets, "build_context_menu_response")
