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

    # --- Vendor / trade state ---
    refused_vendors: dict[int, float] = field(default_factory=dict)
    failed_destinations: dict[tuple[int, int], float] = field(default_factory=dict)

    # --- Intent / UI ---
    planner_intent: str = ""
    current_procedure: str | None = None

    # --- Anything else (legacy keys) ---
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
        "refused_vendors": "refused_vendors",
        "failed_destinations": "_failed_destinations",
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
