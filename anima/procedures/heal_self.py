"""HealSelf — quaff an on-hand healing potion for an instant HP top-up.

The v2 planner already names this procedure as its last-resort survival heal:
``planner.py`` does ``self.registry.get("heal_self")`` in the backpack-less
heal-in-place branch (~L948) and again in the Priority-1 critical-HP ladder
(~L1166), with comments describing it as a heal that "quaffs an on-hand potion
and needs no backpack". Until now NOTHING registered a procedure under that
name in ``Avatar.create()`` — only the legacy ``HealSelf`` *skill* (bandage
based) lived in the separate skill_registry — so both ``registry.get`` calls
returned ``None`` and the entire potion-quaff survival path was dead code. A
critically wounded agent with no usable bandages (a mage, or one whose bandages
were consumed/interrupted) would fall straight through to the profession loop
and bleed to death, exactly the non-negotiable death those planner branches
were written to prevent.

A heal potion is drunk by a plain double-click — NO target cursor — and carries
its own server-side ~10 s use-delay (ServUO ``BaseHealPotion``), so we gate on a
dedicated cooldown key. ServUO refuses a heal potion outright while poisoned
("You can not heal yourself in your current state."), so a poisoned agent yields
to the cure-bandage path instead. We reuse ``HEAL_POTION_GRAPHICS`` /
``POTION_COOLDOWN_S`` from ``combat_loop`` so the constants never drift.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.inventory import find_in_backpack
from anima.client.packets import build_double_click
from anima.procedures.base import FailureReason, Procedure, ProcedureResult
from anima.procedures.combat_loop import HEAL_POTION_GRAPHICS, POTION_COOLDOWN_S

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()

# Quaff once HP is in the danger band. Mirrors the planner's in-place heal floor
# intent (a wounded agent should be able to top up across the whole 30-40% band),
# not the tighter combat-only POTION_HP_PCT — the planner itself decides WHEN to
# select this proc; can_start only refuses an obviously-pointless quaff.
HEAL_POTION_HP_PCT = 90.0


class HealSelf(Procedure):
    timeout_s = 15.0
    name = "heal_self"
    description = "Quaff a healing potion for an instant HP top-up (no backpack layer / target cursor needed)."

    async def can_start(self, ctx: AgentContext) -> bool:
        ss = ctx.perception.self_state
        if not ss.is_alive:
            return False
        if ss.hits_max <= 0:
            return False
        # Only quaff while genuinely wounded — at (near) full HP a potion is wasted.
        if ss.hp_percent >= HEAL_POTION_HP_PCT:
            return False
        # ServUO BaseHealPotion.Drink refuses while poisoned; nothing is consumed
        # and the cure-bandage path handles poison, so yield to it here.
        if bool(getattr(ss, "is_poisoned", False)):
            return False
        # Respect the potion's own server-side use-delay — a drink inside it is
        # refused, so re-selecting would burn the planner tick for a no-op.
        last = ctx.blackboard.get("_potion_last_ts", 0.0) if ctx.blackboard else 0.0
        if time.monotonic() - last < POTION_COOLDOWN_S:
            return False
        return bool(find_in_backpack(ctx, HEAL_POTION_GRAPHICS))

    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        ss = ctx.perception.self_state
        potions = find_in_backpack(ctx, HEAL_POTION_GRAPHICS)
        if not potions:
            return ProcedureResult(
                success=False,
                reason=FailureReason.MISSING_RESOURCE,
                message="No heal potion to quaff",
            )

        # The quaff is a single double-click with no target cursor, so the ONLY
        # proof the potion actually drank is that the packet went out on a LIVE
        # connection — a disconnected ``send_packet`` is a silent no-op (see
        # connection.connected / the same guard in practice_music, combat_loop).
        # If we send into a dead socket anyway the old code (a) stamped
        # ``_potion_last_ts`` — parking the can_start cooldown for the full
        # POTION_COOLDOWN_S so the agent will NOT retry the heal for ~10s — and
        # (b) returned success=True, a phantom win written to the ActionLog
        # reward signal. heal_self is the last-resort survival heal in the
        # Priority-1 critical-HP ladder, so a phantom quaff there is exactly the
        # death those branches exist to prevent: the planner is told the heal
        # landed and the cooldown blocks the only retry. Refuse early as a
        # retryable failure WITHOUT stamping the cooldown, so the moment the
        # session reconnects the heal can fire.
        if not getattr(ctx.conn, "connected", True):
            return ProcedureResult(
                success=False,
                reason=FailureReason.INTERRUPTED,
                message="Heal potion not quaffed — connection down",
            )

        hp_before = ss.hits
        await ctx.conn.send_packet(build_double_click(potions[0].serial))
        ctx.blackboard["_potion_last_ts"] = time.monotonic()

        # The drink resolves instantly server-side; give the 0x11 status push a
        # brief moment to land so the HP delta is observable.
        if ctx.bus is not None:
            try:
                await ctx.bus.wait_for_condition(
                    lambda: ss.hits > hp_before, timeout=2.0,
                )
            except Exception:
                pass

        healed = max(0, ss.hits - hp_before)
        logger.info(
            "heal_self_potion", healed=healed, hp=f"{ss.hits}/{ss.hits_max}",
        )
        # The double-click was accepted and the potion consumed, so the quaff
        # RESOLVED even when healed == 0 (a near-full top-up). Book it as a
        # success with a non-negative signal — distinct from the no-potion
        # MISSING_RESOURCE failure above.
        return ProcedureResult(
            success=True,
            message=f"Heal potion quaffed: +{healed} HP",
        )
