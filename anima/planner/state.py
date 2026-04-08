"""Typed blackboard state for the planner.

Replaces the untyped dict-based state scattered as dozens of string keys.
Unknown keys are preserved in `extras` so legacy code that still uses
dict access doesn't break during migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlannerBlackboard:
    # --- Procedure failure / skip state ---
    craft_bs_fails: int = 0
    craft_bs_material_fails: int = 0
    craft_bs_material_cooldown: float = 0.0
    make_tools_gave_up: bool = False
    skip_procedures: set[str] = field(default_factory=set)

    # --- Mining state ---
    depleted_banks: dict[tuple[int, int], float] = field(default_factory=dict)
    depleted_mines: dict[tuple[int, int], float] = field(default_factory=dict)  # legacy
    exhausted_mines: dict[str, float] = field(default_factory=dict)
    mine_exhausted_until: float = 0.0
    mine_consec_fail: int = 0
    junk_ore_serials: set[int] = field(default_factory=set)
    unsmelable_ore_hues: set[int] = field(default_factory=set)
    ore_pickup_fails: dict[int, int] = field(default_factory=dict)
    small_iron_ore_serials: set[int] = field(default_factory=set)

    # --- Deadlock resolution state ---
    deadlock_recovery_level: int = 0
    deadlock_attempt_count: int = 0

    # --- Vendor / trade state ---
    refused_vendors: dict[int, float] = field(default_factory=dict)

    # --- Intent / UI ---
    planner_intent: str = ""
    current_procedure: str | None = None

    # --- Anything else (legacy keys or runtime object refs) ---
    # Note: CircuitBreaker instances live on ctx.blackboard under keys like
    # `_bank_breaker`, `_craft_material_breaker`, `_ore_pickup_breaker`, but
    # they are object references — not serializable state — and are set up
    # by Planner.run() each session. They are intentionally NOT in _KEY_MAP.
    # Likewise, `self._failed_destinations` lives on the Planner instance
    # itself, not in ctx.blackboard, so it is NOT in _KEY_MAP either.
    extras: dict[str, Any] = field(default_factory=dict)

    # --- Field name mapping: attribute name -> blackboard key ---
    _KEY_MAP = {
        "craft_bs_fails": "_craft_bs_fails",
        "craft_bs_material_fails": "_craft_bs_material_fails",
        "craft_bs_material_cooldown": "_craft_bs_material_cooldown",
        "make_tools_gave_up": "_make_tools_gave_up",
        "skip_procedures": "_skip_procedures",
        "depleted_banks": "depleted_banks",
        "depleted_mines": "depleted_mines",
        "exhausted_mines": "exhausted_mines",
        "mine_exhausted_until": "_mine_exhausted_until",
        "mine_consec_fail": "_mine_consec_fail",
        "junk_ore_serials": "_junk_ore_serials",
        "unsmelable_ore_hues": "_unsmelable_ore_hues",
        "ore_pickup_fails": "_ore_pickup_fails",
        "small_iron_ore_serials": "_small_iron_ore_serials",
        "deadlock_recovery_level": "_deadlock_recovery_level",
        "deadlock_attempt_count": "_deadlock_attempt_count",
        "refused_vendors": "refused_vendors",
        "planner_intent": "planner_intent",
        "current_procedure": "current_procedure",
    }

    @classmethod
    def from_dict(cls, data: dict) -> "PlannerBlackboard":
        kwargs: dict[str, Any] = {}
        extras: dict[str, Any] = {}
        known_keys = set(cls._KEY_MAP.values())
        for attr, key in cls._KEY_MAP.items():
            if key in data:
                kwargs[attr] = data[key]
        for k, v in data.items():
            if k not in known_keys:
                extras[k] = v
        kwargs["extras"] = extras
        return cls(**kwargs)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {}
        for attr, key in self._KEY_MAP.items():
            val = getattr(self, attr)
            # Don't write default-empty containers (reduces noise in
            # serialized state) but do write scalars.
            if isinstance(val, (dict, set, list)) and not val:
                continue
            if isinstance(val, (int, float)) and val == 0:
                continue
            if val is None or val == "":
                continue
            out[key] = val
        out.update(self.extras)
        return out
