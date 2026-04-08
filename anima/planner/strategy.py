"""LLM-backed session strategy selection.

Every ~5 minutes the LLM picks a named strategy for the current
session. The planner filters procedure selection to match.
"""

from __future__ import annotations

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
    STRATEGY_GRIND_MINING: {
        "craft_blacksmith",  # don't interrupt mining to craft
        "sell_to_vendor",    # don't detour to vendors
    },
    STRATEGY_SELL_INVENTORY: {
        "mine_ore",
        "smelt_ore",
        "craft_blacksmith",
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
    seconds, this triggers an LLM call (non-blocking for the planner —
    the call is awaited but the tick loop continues once it returns).
    """

    DEFAULT_STRATEGY = STRATEGY_GRIND_MINING
    MIN_INTERVAL_S = 60.0  # never less than 1 minute

    def __init__(self, interval_s: float = 300.0) -> None:
        self.interval_s = max(interval_s, self.MIN_INTERVAL_S)
        self._current: StrategyDecision = StrategyDecision(
            name=self.DEFAULT_STRATEGY,
            reasoning="initial default before first LLM call",
        )
        self._last_refresh: float = 0.0
        # Strategy filtering is only applied after the first successful LLM
        # decision. Before that, the planner operates without restrictions.
        self._active: bool = False

    @property
    def current(self) -> StrategyDecision:
        return self._current

    def should_refresh(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return now - self._last_refresh >= self.interval_s

    async def maybe_refresh(self, ctx: "AgentContext") -> bool:
        """Refresh the strategy if the interval has elapsed.

        Returns True if a refresh happened (whether or not the strategy
        actually changed).
        """
        if not self.should_refresh():
            return False
        llm = getattr(ctx, "llm", None)
        if llm is None:
            # No LLM configured — stay on default
            self._last_refresh = time.time()
            return False
        try:
            new_decision = await self._ask_llm(ctx, llm)
        except Exception as e:
            logger.warning("strategy_llm_failed", error=str(e))
            self._last_refresh = time.time()
            return False
        if new_decision.name not in ALL_STRATEGIES:
            logger.warning(
                "strategy_llm_unknown_response",
                returned=new_decision.name,
            )
            self._last_refresh = time.time()
            return False
        previous = self._current.name
        self._current = new_decision
        self._last_refresh = time.time()
        self._active = True
        if previous != new_decision.name:
            logger.info(
                "strategy_changed",
                from_=previous,
                to=new_decision.name,
                reasoning=new_decision.reasoning[:100],
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
- grind_mining: focus on mining and smelting, ignore crafting/selling detours
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
