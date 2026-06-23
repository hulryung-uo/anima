"""Target action primitives — use object + wait for target cursor + respond."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from anima.actions.result import ActionResult
from anima.client.packets import build_double_click, build_target_response

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


async def use_on_target(
    ctx: AgentContext,
    tool_serial: int,
    x: int,
    y: int,
    z: int,
    graphic: int = 0,
    timeout: float = 3.0,
) -> ActionResult:
    """Double-click tool, wait for target cursor, respond with ground tile.

    This is the core UO interaction: use_object → wait_target → target_tile.
    Used by mining (pickaxe → rock), smelting (ore → forge), etc.
    """
    # Clear pending target
    ctx.perception.self_state.pending_target = None
    await ctx.conn.send_packet(build_double_click(tool_serial))

    # Wait for target cursor
    result = await wait_for_target(ctx, timeout=timeout)
    if not result.success:
        return result

    cursor_id = result.data.get("cursor_id", 0)
    # Echo the server's cursor flag (0=neutral, 1=harmful, 2=helpful) back on
    # the target response. ``wait_for_target`` surfaces it (commit 79042c3) and
    # ``cast_spell`` already replays it (commit 7a60b31), but this primitive —
    # the use-tool-on-tile path behind mining (pickaxe -> rock) and smelting
    # (ore -> forge) — dropped it, defaulting to 0. Strict servers reject a
    # response whose flag does not match the request, silently no-opping the
    # whole action even though we returned success.
    cursor_flag = result.data.get("cursor_flag", 0)

    # Respond with tile target
    return await target_tile(ctx, cursor_id, x, y, z, graphic, cursor_flag)


async def use_on_object(
    ctx: AgentContext,
    tool_serial: int,
    target_serial: int,
    timeout: float = 3.0,
) -> ActionResult:
    """Double-click tool, wait for target cursor, respond with object target."""
    ctx.perception.self_state.pending_target = None
    await ctx.conn.send_packet(build_double_click(tool_serial))

    result = await wait_for_target(ctx, timeout=timeout)
    if not result.success:
        return result

    cursor_id = result.data.get("cursor_id", 0)
    # Echo the server's requested cursor flag — see use_on_target above. This
    # is the use-tool-on-object path (smelt ore -> forge, tinker tool -> ...).
    cursor_flag = result.data.get("cursor_flag", 0)
    return await target_object(ctx, cursor_id, target_serial, cursor_flag)


async def wait_for_target(
    ctx: AgentContext,
    timeout: float = 3.0,
) -> ActionResult:
    """Wait for the server to send a target cursor (packet 0x6C)."""
    ss = ctx.perception.self_state

    if ctx.bus:
        ok = await ctx.bus.wait_for_condition(
            lambda: ss.pending_target is not None,
            timeout=timeout,
        )
    else:
        # Fallback polling if no bus available
        import asyncio
        for _ in range(int(timeout / 0.1)):
            if ss.pending_target is not None:
                break
            await asyncio.sleep(0.1)
        ok = ss.pending_target is not None

    if not ok:
        return ActionResult(success=False, message="Target cursor timeout")

    # Snapshot the cursor ONCE. ``ok`` reflects the predicate at the instant the
    # bus wait resolved, but ``ss.pending_target`` is mutated by packet handlers
    # running concurrently on the same event loop: a server WITHDRAW cursor
    # (0x6C with ``cursor_flag == 3``, handlers.py) and the living->ghost death
    # transition both null ``pending_target`` (commit 47c4a82). One landing in
    # the window between the wait waking and this coroutine resuming leaves
    # ``pending_target`` None by read time. The old ``ss.pending_target or {}``
    # then silently became an empty dict, so ``cursor_id`` defaulted to 0 and
    # this returned ``success=True`` with a ZERO cursor — every caller
    # (cast_spell / use_on_object / use_on_target / use_skill_on) then fired a
    # target response against a cursor id the server already retired, a no-op
    # the server drops while the action layer reported the targeted action
    # succeeded. Degrade to the normal "Target cursor timeout" failure (which
    # every caller already handles) instead. Mirrors wait_for_gump's TOCTOU
    # guard (commit 20a6943).
    pt = ss.pending_target
    if pt is None:
        return ActionResult(success=False, message="Target cursor timeout")
    cursor_id = pt.get("cursor_id", 0)
    # The 0x6C handler stores "target_type" (0=object, 1=ground) and
    # "cursor_flag" (0=neutral, 1=harmful, 2=helpful). Read those exact
    # keys — the old code read a "cursor_type" key the handler never sets,
    # so it was always 0 and the harmful/helpful flag was silently dropped.
    # Strict servers reject a target response whose flag does not echo the
    # request, so callers must be able to read and replay it.
    target_type = pt.get("target_type", pt.get("cursor_type", 0))
    cursor_flag = pt.get("cursor_flag", 0)
    ss.pending_target = None

    return ActionResult(
        success=True,
        data={
            "cursor_id": cursor_id,
            "target_type": target_type,
            "cursor_flag": cursor_flag,
            # back-compat alias for callers still reading "cursor_type"
            "cursor_type": target_type,
        },
    )


async def target_tile(
    ctx: AgentContext,
    cursor_id: int,
    x: int,
    y: int,
    z: int,
    graphic: int = 0,
    cursor_flag: int = 0,
) -> ActionResult:
    """Send target response for a ground/static tile."""
    await ctx.conn.send_packet(build_target_response(
        target_type=1,
        cursor_id=cursor_id,
        x=x, y=y, z=z,
        graphic=graphic,
        cursor_flag=cursor_flag,
    ))
    logger.debug("target_tile", cursor_id=f"0x{cursor_id:08X}", pos=f"({x},{y},{z})")
    return ActionResult(success=True, data={"x": x, "y": y, "z": z})


async def target_object(
    ctx: AgentContext,
    cursor_id: int,
    serial: int,
    cursor_flag: int = 0,
) -> ActionResult:
    """Send target response for an object/mobile."""
    await ctx.conn.send_packet(build_target_response(
        target_type=0,
        cursor_id=cursor_id,
        serial=serial,
        cursor_flag=cursor_flag,
    ))
    logger.debug("target_object", cursor_id=f"0x{cursor_id:08X}", serial=f"0x{serial:08X}")
    return ActionResult(success=True, data={"serial": serial})


async def cancel_target(ctx: AgentContext) -> ActionResult:
    """Cancel a pending target cursor."""
    ss = ctx.perception.self_state
    if ss.pending_target is None:
        return ActionResult(success=True, message="No target to cancel")

    cursor_id = ss.pending_target.get("cursor_id", 0)
    # Echo back the request's type byte and cursor flag — see below.
    target_type = ss.pending_target.get("target_type", 0)
    cursor_flag = ss.pending_target.get("cursor_flag", 0)
    ss.pending_target = None

    # Send the canonical escape-cancel. ServUO recognises a cursor cancel ONLY
    # via the "User pressed escape" branch (PacketHandlers.cs:1267):
    #   ``x == -1 && y == -1 && !serial.IsValid``
    # where x/y are read as signed Int16, so -1 is the wire value 0xFFFF. That
    # branch is checked BEFORE the ``Target.TargetIDValidation`` guard
    # (PacketHandlers.cs:1272), so an escape-form cancel always clears the
    # cursor even when our cached cursor_id has gone stale. Mirrors ClassicUO
    # ``Send_TargetCancel`` (OutgoingPackets.cs:1781), which writes serial=0 and
    # x/y = 0xFFFF.
    #
    # The old zero-coordinate form (x=0, y=0, serial=0) does NOT hit the escape
    # branch; it only cancels by falling through to the invalid-serial ``else``
    # branch (PacketHandlers.cs:1339) — which sits *behind* the TargetID check,
    # so with TargetIDValidation enabled and a stale cursor_id the server
    # returns without cancelling and the cursor stays pending forever, wedging
    # every subsequent targeted action.
    #
    # The type byte and cursor flag must ECHO the original request, not be
    # hardcoded to 0. ClassicUO's TargetManager.CancelTarget()
    # (TargetManager.cs:210) sends
    #   ``Send_TargetCancel(TargetingState, _targetCursorId, (byte)TargetingType)``
    # i.e. the cursor's own type byte (CursorTarget) and its harmful/helpful
    # flag (TargetingType), both captured when the 0x6C request arrived. The old
    # code wrote type=0/flag=0 unconditionally, so the cancel for a ground
    # cursor (request type 1) or a harmful/helpful cursor went out with the
    # wrong header bytes — a mismatch a flag-validating server can reject,
    # leaving the cursor open and wedging the next targeted action. The 0x6C
    # handler already stashes both fields in ``pending_target`` (handlers.py
    # ~1297), so replay them here.
    await ctx.conn.send_packet(build_target_response(
        target_type=target_type, cursor_id=cursor_id, serial=0,
        x=0xFFFF, y=0xFFFF, cursor_flag=cursor_flag,
    ))
    return ActionResult(success=True)
