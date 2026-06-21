"""LLM-backed session strategy selection.

Every ~5 minutes the LLM picks a named strategy for the current
session. The planner filters procedure selection to match.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


# Named strategies — ordered roughly by commonality
STRATEGY_GRIND_MINING   = "grind_mining"
STRATEGY_SELL_INVENTORY = "sell_inventory"
STRATEGY_BANK_COLORED   = "bank_colored"
STRATEGY_UPGRADE_TOOLS  = "upgrade_tools"
STRATEGY_FILL_COFFERS   = "fill_coffers"

ALL_STRATEGIES = {
    STRATEGY_GRIND_MINING,
    STRATEGY_SELL_INVENTORY,
    STRATEGY_BANK_COLORED,
    STRATEGY_UPGRADE_TOOLS,
    STRATEGY_FILL_COFFERS,
}


# Which procedures each strategy EXCLUDES from selection.
# If a strategy isn't listed, no filtering is applied.
STRATEGY_EXCLUSIONS: dict[str, set[str]] = {
    # grind_mining has no exclusions: the full mining loop is
    # mine → smelt → craft → sell → mine. Blocking any step (craft OR sell)
    # causes ingots to accumulate until the agent is overweight with no way
    # to dispose of them, OR makes the planner ping-pong between forge
    # locations looking for one where crafting is "allowed".
    STRATEGY_GRIND_MINING: set(),
    STRATEGY_SELL_INVENTORY: {
        # Don't gather more — but smelting and crafting are prerequisites
        # for actually selling what's already in the backpack (raw ingots
        # have no dedicated sell path outside the no-tool case; crafted
        # items do). Excluding them strands inventory-heavy ingots.
        "mine_ore",
    },
    STRATEGY_BANK_COLORED: {
        "mine_ore",
        "craft_blacksmith",
    },
    STRATEGY_UPGRADE_TOOLS: {
        "mine_ore",
        "craft_blacksmith",
    },
    # Nothing excluded — whatever makes gold fastest
    STRATEGY_FILL_COFFERS: set(),
}


@dataclass
class StrategyDecision:
    name: str
    reasoning: str
    chosen_at: float = field(default_factory=time.time)


class StrategySelector:
    """Holds the current session strategy and refreshes it periodically.

    The planner calls `maybe_refresh()` every tick. Every `interval_s`
    seconds, this spawns a background task that asks the LLM for a new
    strategy. The planner loop never awaits the LLM call — `maybe_refresh`
    returns immediately after spawning so a slow model (e.g. cold-start
    on a remote 235B Qwen) cannot stall procedure selection or trigger
    a connection-silence timeout.
    """

    DEFAULT_STRATEGY = STRATEGY_GRIND_MINING
    MIN_INTERVAL_S = 60.0  # never less than 1 minute

    def __init__(self, interval_s: float = 300.0) -> None:
        self.interval_s = max(interval_s, self.MIN_INTERVAL_S)
        self._current: StrategyDecision = StrategyDecision(
            name=self.DEFAULT_STRATEGY,
            reasoning="initial default before first LLM call",
        )
        # Monotonic timestamp of the last refresh spawn. ``None`` means
        # "never refreshed yet" so the first tick is always eligible — we
        # must NOT seed this with 0.0 and read a monotonic clock, because
        # ``time.monotonic()`` starts from an arbitrary (often small, e.g.
        # seconds-since-boot) origin: on a freshly booted host ``now - 0.0``
        # could be < interval_s and silently suppress the first refresh.
        self._last_refresh: float | None = None
        # Strategy filtering is only applied after the first successful LLM
        # decision. Before that, the planner operates without restrictions.
        self._active: bool = False
        # Single-flight guard: at most one refresh task in flight.
        self._refresh_task: asyncio.Task | None = None

    @property
    def current(self) -> StrategyDecision:
        return self._current

    def should_refresh(self, now: float | None = None) -> bool:
        # Cadence gate runs on a MONOTONIC clock, not wall time. The interval
        # is a "seconds since the last refresh" throttle, and wall time
        # (``time.time()``) is not monotonic: an NTP correction or a VM
        # resume can step it BACKWARD by minutes, making ``now - last`` go
        # negative and the gate stay closed for the whole backward step —
        # silently freezing strategy refreshes (and, by the same pattern in
        # MetaController, the P0 shadow-decision log). ``time.monotonic()``
        # is immune to clock steps.
        if self._last_refresh is None:
            return True  # never refreshed → always eligible on the first tick
        now = now if now is not None else time.monotonic()
        return now - self._last_refresh >= self.interval_s

    async def maybe_refresh(self, ctx: "AgentContext") -> bool:
        """Spawn a background strategy-refresh task if eligible.

        Returns True if a refresh task was spawned this call. Returns
        False if the interval has not elapsed or another refresh is
        already in flight. The actual LLM call runs concurrently on
        `self._refresh_task`; await it directly if the caller needs the
        result (tests do this).

        The interval gate (`_last_refresh`) is bumped immediately on
        spawn so the planner does not re-spawn every tick while the LLM
        is still working — and so a hung/failing LLM does not turn into
        a tight retry loop.
        """
        if not self.should_refresh():
            return False
        if self._refresh_task is not None and not self._refresh_task.done():
            return False
        self._last_refresh = time.monotonic()
        self._refresh_task = asyncio.create_task(self._do_refresh(ctx))
        return True

    async def _do_refresh(self, ctx: "AgentContext") -> None:
        """Run the LLM strategy decision and apply the result.

        Strategy changes are suppressed while the expedition is in the
        middle of an active COLLECTING or CRAFTING_TRIP cycle — the LLM
        was observed interrupting a productive run mid-sequence (picking
        sell_inventory while the agent was smelting at the forge), which
        stranded the in-progress batch. Only re-evaluate at natural
        boundaries: IDLE or MINING phase.
        """
        expedition = ctx.blackboard.get("expedition") if hasattr(ctx, "blackboard") else None
        if expedition is not None:
            phase_val = getattr(getattr(expedition, "phase", None), "value", None)
            if phase_val in {"collecting", "crafting_trip"}:
                logger.debug(
                    "strategy_refresh_deferred",
                    phase=phase_val,
                    current=self._current.name,
                )
                return

        llm = getattr(ctx, "llm", None)
        if llm is None:
            return
        try:
            new_decision = await self._ask_llm(ctx, llm)
        except Exception as e:
            logger.warning("strategy_llm_failed", error=str(e))
            return
        if new_decision.name not in ALL_STRATEGIES:
            logger.warning(
                "strategy_llm_unknown_response",
                returned=new_decision.name,
            )
            return

        # Sanity-check the LLM choice against actual state. The model
        # sometimes picks "sell_inventory" with an empty backpack or
        # "bank_colored" without colored ingots, which then blocks
        # mining and strands the agent. Override with grind_mining when
        # the chosen strategy is incompatible with current inventory.
        if not self._is_strategy_viable(ctx, new_decision.name):
            logger.info(
                "strategy_rejected",
                llm_choice=new_decision.name,
                reason="state incompatible with strategy preconditions",
                fallback=self.DEFAULT_STRATEGY,
            )
            new_decision = StrategyDecision(
                name=self.DEFAULT_STRATEGY,
                reasoning=f"auto-fallback: {new_decision.name} not viable",
            )

        previous = self._current.name
        self._current = new_decision
        self._active = True
        if previous != new_decision.name:
            logger.info(
                "strategy_changed",
                from_=previous,
                to=new_decision.name,
                reasoning=new_decision.reasoning[:100],
            )

    def _is_strategy_viable(self, ctx: "AgentContext", name: str) -> bool:
        """Reject strategies whose preconditions are obviously not met."""
        if name == STRATEGY_GRIND_MINING:
            return True  # always viable; default
        if name == STRATEGY_FILL_COFFERS:
            return True  # broad enough to be always plausible
        ss = ctx.perception.self_state
        backpack = ss.equipment.get(0x15) if hasattr(ss.equipment, "get") else None
        items = list(ctx.perception.world.items.values())

        if name == STRATEGY_UPGRADE_TOOLS:
            # Upgrading requires gold to buy (≥10gp) OR ingots to craft (≥4).
            # Without either, this strategy excludes mine_ore but provides no
            # alternative — stranding the agent in an idle loop.
            if ss.gold >= 10:
                return True
            from anima.skills.crafting.smelt import INGOT_GRAPHICS
            ingot_count = sum(
                getattr(it, "amount", 1)
                for it in items
                if getattr(it, "container", 0) == backpack
                and getattr(it, "graphic", 0) in INGOT_GRAPHICS
            )
            return ingot_count >= 4

        if name == STRATEGY_SELL_INVENTORY:
            # Need at least one *directly* sellable thing in backpack.
            # Raw ore requires processing (smelt → craft → sell) which is
            # the grind_mining pipeline — counting it as sellable strands
            # the agent when mine_ore is excluded by this strategy.
            from anima.skills.crafting.smelt import INGOT_GRAPHICS
            from anima.procedures.craft_blacksmith import CRAFTED_ITEM_GRAPHICS
            sellable_graphics = INGOT_GRAPHICS | CRAFTED_ITEM_GRAPHICS
            return any(
                getattr(it, "container", 0) == backpack
                and getattr(it, "graphic", 0) in sellable_graphics
                for it in items
            )

        if name == STRATEGY_BANK_COLORED:
            from anima.skills.crafting.smelt import INGOT_GRAPHICS
            return any(
                getattr(it, "container", 0) == backpack
                and getattr(it, "graphic", 0) in INGOT_GRAPHICS
                and getattr(it, "hue", 0) != 0  # non-iron
                for it in items
            )

        return True

    async def _ask_llm(self, ctx: "AgentContext", llm) -> StrategyDecision:
        """Build a short prompt and parse the LLM response.

        Expected response format:
            STRATEGY: <name>
            REASONING: <one-line why>
        """
        ss = ctx.perception.self_state
        # Compact inventory summary (graphic + amount for top 5)
        backpack = ss.equipment.get(0x15) if hasattr(ss.equipment, "get") else None
        inv_summary: list[str] = []
        if backpack is not None:
            for it in list(ctx.perception.world.items.values())[:30]:
                if getattr(it, "container", 0) == backpack:
                    name = getattr(it, "name", None) or f"0x{it.graphic:04X}"
                    inv_summary.append(
                        f"{getattr(it, 'amount', 1)}x {name}"
                    )
                if len(inv_summary) >= 10:
                    break

        inventory_text = ", ".join(inv_summary) or "(empty)"

        prompt = f"""You are directing an Ultima Online mining agent. Pick the best strategy for the NEXT 5 MINUTES.

Current state:
- Gold: {ss.gold}
- Weight: {ss.weight}/{ss.weight_max}
- Inventory: {inventory_text}

Available strategies:
- grind_mining: full mining loop — mine, smelt, craft weapons/armor, sell them for gold
- sell_inventory: stop gathering, clear the backpack to a vendor
- bank_colored: deposit non-iron ingots at the bank
- upgrade_tools: buy or craft new pickaxes / tongs
- fill_coffers: maximize gold income however possible

Respond in EXACTLY this format:
STRATEGY: <one of the names above>
REASONING: <one short sentence>"""

        response = await llm.chat([
            {"role": "user", "content": prompt},
        ])
        text = (response.text or "").strip()
        # Parse
        name = self.DEFAULT_STRATEGY
        reasoning = "LLM parse failed"
        for line in text.splitlines():
            line = line.strip()
            if line.upper().startswith("STRATEGY:"):
                name = line.split(":", 1)[1].strip().lower()
            elif line.upper().startswith("REASONING:"):
                reasoning = line.split(":", 1)[1].strip()
        return StrategyDecision(name=name, reasoning=reasoning)

    def is_excluded(self, procedure_name: str) -> bool:
        """True if `procedure_name` is excluded by the current strategy.

        Returns False until the first successful LLM refresh so that the
        planner operates without restrictions before the agent has enough
        context to choose a strategy.
        """
        if not self._active:
            return False
        excl = STRATEGY_EXCLUSIONS.get(self._current.name, set())
        return procedure_name in excl
