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

    # Respond with tile target
    return await target_tile(ctx, cursor_id, x, y, z, graphic)


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
    return await target_object(ctx, cursor_id, target_serial)


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

    cursor_id = ss.pending_target.get("cursor_id", 0) if ss.pending_target else 0
    cursor_type = ss.pending_target.get("cursor_type", 0) if ss.pending_target else 0
    ss.pending_target = None

    return ActionResult(
        success=True,
        data={"cursor_id": cursor_id, "cursor_type": cursor_type},
    )


async def target_tile(
    ctx: AgentContext,
    cursor_id: int,
    x: int,
    y: int,
    z: int,
    graphic: int = 0,
) -> ActionResult:
    """Send target response for a ground/static tile."""
    await ctx.conn.send_packet(build_target_response(
        target_type=1,
        cursor_id=cursor_id,
        x=x, y=y, z=z,
        graphic=graphic,
    ))
    logger.debug("target_tile", cursor_id=f"0x{cursor_id:08X}", pos=f"({x},{y},{z})")
    return ActionResult(success=True, data={"x": x, "y": y, "z": z})


async def target_object(
    ctx: AgentContext,
    cursor_id: int,
    serial: int,
) -> ActionResult:
    """Send target response for an object/mobile."""
    await ctx.conn.send_packet(build_target_response(
        target_type=0,
        cursor_id=cursor_id,
        serial=serial,
    ))
    logger.debug("target_object", cursor_id=f"0x{cursor_id:08X}", serial=f"0x{serial:08X}")
    return ActionResult(success=True, data={"serial": serial})


async def cancel_target(ctx: AgentContext) -> ActionResult:
    """Cancel a pending target cursor."""
    ss = ctx.perception.self_state
    if ss.pending_target is None:
        return ActionResult(success=True, message="No target to cancel")

    cursor_id = ss.pending_target.get("cursor_id", 0)
    ss.pending_target = None

    # Send cancel (target_type=0, serial=0, coords=0)
    await ctx.conn.send_packet(build_target_response(
        target_type=0, cursor_id=cursor_id,
    ))
    return ActionResult(success=True)
