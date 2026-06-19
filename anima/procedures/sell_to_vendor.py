"""SellToVendor procedure — sell items to NPC vendor via context menu."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.skills.trade.vendor import (
    _CLILOC_VENDOR_SELL,
    KEEP_GRAPHICS,
    _find_vendor,
    _mark_refused,
    _request_context_menu_entry,
)

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


class SellToVendor(Procedure):
    name = "sell_to_vendor"
    description = "Sell items to a nearby NPC vendor."

    @staticmethod
    def _dynamic_keep(ctx: AgentContext) -> set[int]:
        """KEEP_GRAPHICS, releasing ingots only as a dead-broke last resort.

        Ingots are the smithing training feedstock — selling them stalls
        the craft→sell loop (a transient material cooldown or standing a
        few tiles from the forge used to drop protection and the agent
        sold its training ingots). They lose protection only when crafting
        is truly dead AND we cannot raise gold any other way: no tongs in
        the pack, no crafted items left to sell, and gold below the
        emergency floor (enough to rebuy tongs).
        """
        from anima.actions.inventory import find_in_backpack
        from anima.procedures.craft_blacksmith import (
            CRAFTED_ITEM_GRAPHICS as _CRAFTED_GFX,
        )
        from anima.procedures.craft_blacksmith import (
            TONGS_GRAPHICS as _TONGS_GFX,
        )

        keep = set(KEEP_GRAPHICS)
        ss = ctx.perception.self_state
        has_tongs = bool(find_in_backpack(ctx, _TONGS_GFX))
        has_crafted = bool(find_in_backpack(ctx, _CRAFTED_GFX))
        broke = ss.gold < 50
        if not has_tongs and not has_crafted and broke:
            for _ig in (0x1BF2, 0x1BEF, 0x1BF0, 0x1BF1):
                keep.discard(_ig)  # last resort: sell ingots to rebuy tools
        return keep

    @staticmethod
    def _select_sellable(
        sell_list: list,
        effective_keep: set[int],
        protected_serials: set[int],
    ) -> list:
        """Pick which sell-list items to actually sell, highest value first.

        Filters out:
        - items whose graphic is still protected (``effective_keep``),
        - the one serial we reserve per surplus tool group,
        - **price-0 items**.

        The 0x9E sell list ServUO sends is built purely from
        ``IShopSellInfo.IsSellable`` (BaseVendor.cs ~L1154) and does NOT
        filter on sell price, so it routinely contains items the vendor
        values at 0gp. ServUO's OnVendorSell still *consumes* (deletes /
        dumps to the vendor BuyPack) any item we send for it while adding
        ``singlePrice * amount == 0`` gold (BaseVendor.cs ~L2196-2207).
        Selling those would silently destroy the agent's items for nothing,
        so we never put a 0-price item in the 0x9F sell request.
        """
        return sorted(
            [si for si in sell_list
             if si.graphic not in effective_keep
             and si.serial not in protected_serials
             and si.price > 0],
            key=lambda si: si.price * si.amount,
            reverse=True,
        )

    @staticmethod
    def _desired_vendor_types(ctx: AgentContext, keep: set[int]) -> set[str] | None:
        """Vendor types that buy our sellable items, or None for any vendor.

        Also routes for surplus crafting tools (count ≥ 2 per graphic):
        the keep-at-least-1 logic below lets us sell duplicates even
        though they're in KEEP_GRAPHICS, so the vendor we walk to must
        accept them (e.g. a tinker for extra tinker's tools).
        """
        from anima.procedures.craft_blacksmith import TONGS_GRAPHICS as _TONGS_GFX
        from anima.procedures.vendor_knowledge import ITEM_VENDOR_MAP
        from anima.skills.crafting.tinker import (
            HATCHET_GRAPHICS as _HATCH_GFX,
        )
        from anima.skills.crafting.tinker import (
            PICKAXE_GRAPHICS as _PICK_GFX,
        )
        from anima.skills.crafting.tinker import (
            SAW_GRAPHICS as _SAW_GFX,
        )
        from anima.skills.crafting.tinker import (
            TINKER_TOOLS_GRAPHICS as _TINKER_GFX,
        )

        ss = ctx.perception.self_state
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return None

        pack_items = [
            it for it in ctx.perception.world.items.values()
            if it.container == backpack
        ]

        types: set[str] = set()
        for it in pack_items:
            if it.graphic not in keep:
                vt = ITEM_VENDOR_MAP.get(it.graphic)
                if vt:
                    types.update(vt)

        for group in (_TONGS_GFX, _TINKER_GFX, _PICK_GFX, _HATCH_GFX, _SAW_GFX):
            group_items = [it for it in pack_items if it.graphic in group]
            if len(group_items) >= 2:
                for it in group_items:
                    vt = ITEM_VENDOR_MAP.get(it.graphic)
                    if vt:
                        types.update(vt)

        return types or None

    async def can_start(self, ctx: AgentContext) -> bool:
        keep = self._dynamic_keep(ctx)
        vendor_types = self._desired_vendor_types(ctx, keep)
        vendor = _find_vendor(ctx, vendor_types=vendor_types)
        return vendor is not None

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        gold_before = ss.gold

        keep = self._dynamic_keep(ctx)
        vendor_types = self._desired_vendor_types(ctx, keep)

        vendor = _find_vendor(ctx, vendor_types=vendor_types)
        if not vendor:
            logger.info("sell_vendor_not_found", pos=f"({ss.x},{ss.y})",
                        vendor_types=vendor_types)
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message="no vendor nearby",
            )

        vendor_name = vendor.name or "vendor"

        # Log what we have to sell
        backpack = ss.equipment.get(0x15)
        sellable_items: list[str] = []
        if backpack:
            from anima.data import item_name as _item_name
            for it in ctx.perception.world.items.values():
                if it.container == backpack and it.graphic not in keep:
                    name = it.name or _item_name(it.graphic) or f"0x{it.graphic:04X}"
                    sellable_items.append(f"{name} x{it.amount}" if it.amount > 1 else name)

        logger.info(
            "sell_attempt",
            vendor=vendor_name,
            vendor_pos=f"({vendor.x},{vendor.y})",
            player_pos=f"({ss.x},{ss.y})",
            gold_before=gold_before,
            sellable_items=sellable_items[:10],
            sellable_count=len(sellable_items),
        )

        # Request sell menu via context menu
        ok = await _request_context_menu_entry(ctx, vendor, _CLILOC_VENDOR_SELL)
        if not ok:
            _mark_refused(ctx, vendor.serial)
            logger.warning(
                "sell_no_menu",
                vendor=vendor_name,
                reason="context menu did not have Sell option",
            )
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"vendor did not respond to sell request ({vendor_name})",
            )

        # Wait for sell list, watch for rejection speech
        _rejected = {"hit": False, "text": ""}
        def _check_rejection(_topic: str, data: dict) -> None:
            text = data.get("text", "").lower()
            if "nothing i would be interested" in text or "not interested" in text:
                _rejected["hit"] = True
                _rejected["text"] = data.get("text", "")

        sub = None
        if ctx.bus:
            sub = ctx.bus.subscribe("avatar.speech_heard", _check_rejection)

        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            if ss.vendor_sell_list or _rejected["hit"]:
                break
            await asyncio.sleep(0.2)

        if sub and ctx.bus:
            ctx.bus.unsubscribe(sub)

        if _rejected["hit"]:
            # This vendor doesn't want our items — mark and try elsewhere
            _mark_refused(ctx, vendor.serial)
            logger.warning(
                "sell_rejected",
                vendor=vendor_name,
                rejection_text=_rejected["text"],
                our_items=sellable_items[:5],
                reason="vendor not interested — wrong vendor type for these items?",
            )
            return ProcedureResult(
                success=False,
                reason=FailureReason.WRONG_LOCATION,
                message=f"{vendor_name} not interested in our items",
                details={
                    "vendor": vendor_name,
                    "rejection": _rejected["text"],
                    "our_items": sellable_items[:5],
                },
            )

        if not ss.vendor_sell_list:
            logger.warning(
                "sell_no_list",
                vendor=vendor_name,
                reason="sell list never arrived (timeout 3s)",
            )
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"sell list did not appear from {vendor_name}",
            )

        # Log what the vendor is willing to buy
        sell_list = ss.vendor_sell_list
        vendor_offers: list[str] = []
        for si in sell_list:
            name = si.name or f"0x{si.graphic:04X}"
            kept = si.graphic in keep
            tag = " [KEEP]" if kept else ""
            vendor_offers.append(
                f"{name} x{si.amount} @{si.price}gp{tag}"
            )

        logger.info(
            "sell_list_received",
            vendor=vendor_name,
            total_items=len(sell_list),
            vendor_offers=vendor_offers,
        )

        # --- Tool keep-at-least-1 logic ---
        # KEEP_GRAPHICS blocks ALL of a tool type from being sold.
        # But if we have 2+ of a type, surplus can be sold. We protect
        # exactly 1 serial per tool group and allow the rest through.
        from anima.procedures.craft_blacksmith import TONGS_GRAPHICS as _TONGS_GFX
        from anima.skills.crafting.tinker import (
            HATCHET_GRAPHICS as _HATCH_GFX,
        )
        from anima.skills.crafting.tinker import (
            PICKAXE_GRAPHICS as _PICK_GFX,
        )
        from anima.skills.crafting.tinker import (
            SAW_GRAPHICS as _SAW_GFX,
        )
        from anima.skills.crafting.tinker import (
            TINKER_TOOLS_GRAPHICS as _TINKER_GFX,
        )

        _TOOL_GROUPS = [_TONGS_GFX, _TINKER_GFX, _PICK_GFX, _HATCH_GFX, _SAW_GFX]
        protected_serials: set[int] = set()
        relaxed_graphics: set[int] = set()
        for group in _TOOL_GROUPS:
            group_items = [si for si in sell_list if si.graphic in group]
            if len(group_items) >= 2:
                # Keep 1, allow selling the rest
                protected_serials.add(group_items[0].serial)
                relaxed_graphics |= group

        # Effective keep: original keep minus relaxed (surplus) tool graphics
        effective_keep = keep - relaxed_graphics

        # --- Sort by value (highest first) and build sell list ---
        from anima.client.packets import build_sell_items

        sellable = self._select_sellable(
            sell_list,
            effective_keep,
            protected_serials,
        )
        items_to_sell = [(si.serial, si.amount) for si in sellable]
        items_kept = [
            si.name or f"0x{si.graphic:04X}" for si in sell_list
            if si.graphic in effective_keep or si.serial in protected_serials
        ]
        expected_gold = sum(si.price * si.amount for si in sellable)

        if not items_to_sell:
            _mark_refused(ctx, vendor.serial)
            logger.info(
                "sell_nothing_to_sell",
                vendor=vendor_name,
                all_items_kept=items_kept,
                reason="all items in sell list are protected (KEEP_GRAPHICS)",
            )
            ss.vendor_sell_list = []
            ss.vendor_serial = 0
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=f"nothing to sell — all items protected ({', '.join(items_kept[:3])})",
            )

        sold_names = [
            f"{si.name or f'0x{si.graphic:04X}'} x{si.amount} @{si.price}gp"
            for si in sellable
        ]

        logger.info(
            "sell_sending",
            vendor=vendor_name,
            items_count=len(items_to_sell),
            expected_gold=expected_gold,
            selling=sold_names,
            keeping=items_kept if items_kept else None,
        )

        await ctx.conn.send_packet(build_sell_items(
            vendor.serial,
            items_to_sell,
        ))

        # Poll for gold change instead of fixed sleep
        from anima.skills.trade.vendor import _wait_for_gold_change
        await _wait_for_gold_change(ss, gold_before, timeout=2.0)

        # Verify gold changed
        gold_after = ss.gold
        gold_earned = max(0, gold_after - gold_before)

        # Clear vendor state
        ss.vendor_sell_list = []
        ss.vendor_serial = 0

        logger.info(
            "sell_result",
            vendor=vendor_name,
            items_sold=len(items_to_sell),
            gold_before=gold_before,
            gold_after=gold_after,
            gold_earned=gold_earned,
            expected_gold=expected_gold,
            sold=sold_names,
        )

        if gold_earned == 0 and expected_gold > 0:
            # The sell packet was sent but gold never moved — the server
            # rejected it (out of range, vendor closed, transient). This is
            # NOT a success: returning success=True here writes a phantom win
            # to the ActionLog reward signal and the planner won't retry the
            # sell. Report it as a retryable BLOCKED failure instead.
            logger.warning(
                "sell_no_gold_received",
                vendor=vendor_name,
                reason="sent sell packet but gold did not change — packet rejected?",
                expected=expected_gold,
            )
            return ProcedureResult(
                success=False,
                reason=FailureReason.BLOCKED,
                message=(
                    f"sell to {vendor_name} sent but gold did not change "
                    f"(expected ~{expected_gold}gp, got 0)"
                ),
                gold_changed=0,
                details={
                    "vendor": vendor_name,
                    "items_sold": sold_names,
                    "gold_earned": 0,
                    "expected_gold": expected_gold,
                },
            )

        msg = f"Sold {len(items_to_sell)} items to {vendor_name}, earned {gold_earned}gp"

        return ProcedureResult(
            success=True,
            message=msg,
            gold_changed=gold_earned,
            details={
                "vendor": vendor_name,
                "items_sold": sold_names,
                "gold_earned": gold_earned,
                "expected_gold": expected_gold,
            },
        )
