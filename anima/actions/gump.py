"""Gump action primitives — wait for gump, click buttons."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from anima.actions.result import ActionResult
from anima.client.packets import build_gump_response

if TYPE_CHECKING:
    from anima.core.context import AgentContext
    from anima.perception.gump import GumpData

logger = structlog.get_logger()


def sanitize_gump_response(
    gump: GumpData,
    button_id: int,
    switches: list[int] | None = None,
    text_entries: list[tuple[int, str]] | None = None,
) -> tuple[int, list[int], list[tuple[int, str]]]:
    """Coerce a gump response to one ServUO will accept (never disconnect).

    ServUO's ``PacketHandlers.DisplayGumpResponse`` calls ``state.Dispose()``
    (which DISCONNECTS the client) on three malformed-response conditions
    beyond the already-handled 239-char text clamp:

    1. ``buttonID`` is neither ``0`` (always 'close') nor a real
       ``GumpButton``/``GumpImageTileButton`` ``ButtonID`` in the gump.
    2. ``switchCount`` exceeds ``gump.m_Switches`` (the number of switch
       entries the server built into the gump).
    3. ``textCount`` exceeds ``gump.m_TextEntries``.

    The dedicated resurrection path guards (1) via ``_safe_button``, but the
    generic crafting/banking/tinker flows route arbitrary button/switch/text
    payloads through :func:`click_gump_button` with no validation, so a stale
    or mis-parsed gump could wedge the eval session behind a disconnect.

    Returns a ``(button_id, switches, text_entries)`` triple safe to send:
    an out-of-range button collapses to ``0``; switch IDs that aren't real
    switches in the gump are dropped; excess text entries are truncated to
    the count the gump actually declared.
    """
    switches = list(switches or [])
    text_entries = list(text_entries or [])

    # (1) button must be 0 or an existing reply/tile button.
    if button_id != 0:
        valid_buttons = {b.button_id for b in gump.buttons}
        if button_id not in valid_buttons:
            logger.warning(
                "gump_response_invalid_button_coerced",
                gump_id=f"0x{gump.gump_id:08X}",
                requested=button_id,
                valid=sorted(valid_buttons),
            )
            button_id = 0

    # (2) only real switch IDs, and never more than the gump declared.
    valid_switches = {s.switch_id for s in gump.switches}
    kept_switches = [s for s in switches if s in valid_switches]
    if len(kept_switches) != len(switches):
        logger.warning(
            "gump_response_switches_filtered",
            gump_id=f"0x{gump.gump_id:08X}",
            requested=switches,
            kept=kept_switches,
        )
    switches = kept_switches[: len(gump.switches)]

    # (3) never more text entries than the gump declared.
    max_entries = len(gump.text_entries)
    if len(text_entries) > max_entries:
        logger.warning(
            "gump_response_text_entries_truncated",
            gump_id=f"0x{gump.gump_id:08X}",
            requested=len(text_entries),
            allowed=max_entries,
        )
        text_entries = text_entries[:max_entries]

    return button_id, switches, text_entries


async def wait_for_gump(
    ctx: AgentContext,
    timeout: float = 3.0,
) -> ActionResult:
    """Wait for a gump to appear."""
    ss = ctx.perception.self_state

    def _has_gump() -> bool:
        return bool(ss.gumps)

    if ctx.bus:
        ok = await ctx.bus.wait_for_condition(_has_gump, timeout=timeout)
    else:
        import asyncio
        for _ in range(int(timeout / 0.1)):
            if _has_gump():
                break
            await asyncio.sleep(0.1)
        ok = _has_gump()

    if not ok:
        return ActionResult(success=False, message="Gump timeout")

    # Return the most recently RECEIVED gump.
    #
    # ``ss.gumps`` is keyed by the gump TYPE id (the second 0xB0/0xDD field),
    # not by recency. The old ``max(ss.gumps.keys())`` returned whichever type
    # id was numerically largest — fine when a single craft gump keeps
    # re-sending under an incrementing id, but wrong the moment an UNRELATED
    # gump with a higher type id lingers alongside the one that just opened
    # (e.g. a status/paperdoll gump still on screen when a vendor/res gump
    # arrives). ``craft_via_gump`` and other callers then drove their click
    # against the stale gump's serial/buttons, so the response either matched
    # no button (ServUO disconnects the client) or operated on the wrong gump.
    # Python dicts preserve insertion order and the 0xB0/0xDD handlers insert a
    # fresh key per newly-opened gump, so the last key is the newest arrival —
    # exactly the "most recent" this function documents.
    gump_id = next(reversed(ss.gumps))
    gump = ss.gumps[gump_id]
    return ActionResult(
        success=True,
        data={"gump_id": gump_id, "gump": gump},
    )


async def click_gump_button(
    ctx: AgentContext,
    gump: GumpData,
    button_id: int,
    switches: list[int] | None = None,
    text_entries: list[tuple[int, str]] | None = None,
) -> ActionResult:
    """Click a button on an open gump.

    The response is sanitized against the open gump's parsed layout so a
    stale/mis-parsed button, switch, or text-entry payload can never trip
    ServUO's "Invalid gump response, disconnecting..." path.
    """
    button_id, switches, text_entries = sanitize_gump_response(
        gump, button_id, switches, text_entries
    )
    pkt = build_gump_response(
        serial=gump.serial,
        gump_id=gump.gump_id,
        button_id=button_id,
        switches=switches,
        text_entries=text_entries,
    )
    await ctx.conn.send_packet(pkt)

    # Remove the gump from state
    ss = ctx.perception.self_state
    ss.gumps.pop(gump.gump_id, None)

    logger.debug(
        "gump_button_clicked",
        gump_id=f"0x{gump.gump_id:08X}",
        button=button_id,
    )
    return ActionResult(success=True, data={"button_id": button_id})


async def craft_via_gump(
    ctx: AgentContext,
    tool_serial: int,
    category_button: int,
    item_button: int,
    timeout: float = 3.0,
) -> ActionResult:
    """Composite: use tool → wait gump → click category → wait gump → click item.

    Used by blacksmithy, tinkering, carpentry crafting gumps.
    """
    from anima.client.packets import build_double_click

    # Open crafting gump
    await ctx.conn.send_packet(build_double_click(tool_serial))

    # Wait for crafting gump
    result = await wait_for_gump(ctx, timeout=timeout)
    if not result.success:
        return ActionResult(success=False, message="Crafting gump did not open")

    gump = result.data["gump"]

    # Click category
    result = await click_gump_button(ctx, gump, category_button)
    if not result.success:
        return result

    # Wait for category sub-gump
    result = await wait_for_gump(ctx, timeout=timeout)
    if not result.success:
        return ActionResult(success=False, message="Category gump did not open")

    gump = result.data["gump"]

    # Click specific item
    result = await click_gump_button(ctx, gump, item_button)
    if not result.success:
        return result

    return ActionResult(success=True, message="Craft command sent")
