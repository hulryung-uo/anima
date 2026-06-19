"""make_tools._decide_craft_target — resource-gating: restock the missing tool.

Regression guard for the bug where a low-Tinkering agent routed to make_tools
to replace a worn-out pickaxe would craft Tinker's Tools forever (skill < 25
short-circuit) and never produce the pickaxe the gather loop was waiting on,
leaving mining permanently stalled.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from anima.procedures.make_tools import MakeTools
from anima.skills.crafting.smelt import INGOT_GRAPHICS
from anima.skills.crafting.tinker import (
    HATCHET_GRAPHICS,
    PICKAXE_GRAPHICS,
    TINKER_TOOLS_GRAPHICS,
)

PICKAXE = next(iter(PICKAXE_GRAPHICS))
HATCHET = next(iter(HATCHET_GRAPHICS))
TINKER = next(iter(TINKER_TOOLS_GRAPHICS))
INGOT = next(iter(INGOT_GRAPHICS))


def _make_ctx(tinker_skill: float, graphics: list[int]):
    """Build a ctx whose backpack holds the given item graphics + ample iron."""
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.equipment = {0x15: 0x101}  # backpack serial
    ctx.blackboard = {}

    items: dict[int, MagicMock] = {}
    serial = 0x1000
    for g in graphics:
        items[serial] = MagicMock(container=0x101, graphic=g, amount=1, hue=0,
                                  serial=serial)
        serial += 1
    # 50 iron ingots (hue 0) so ingot count never gates the decision.
    items[serial] = MagicMock(container=0x101, graphic=INGOT, amount=50, hue=0,
                              serial=serial)
    ctx.perception.world.items = items

    skill = MagicMock()
    skill.id = 37  # Tinkering
    skill.value = tinker_skill
    ss.skills = {37: skill}
    return ctx


def test_low_skill_missing_pickaxe_crafts_pickaxe_not_tinker_tools():
    """The actual bug: skill < 25, no pickaxe, but two spare tinker tools.

    The old code returned 'Tinker's Tools' (skill short-circuit) and never
    replaced the worn-out pickaxe, so the gather→smelt→craft loop stayed dead.
    """
    ctx = _make_ctx(tinker_skill=10.0, graphics=[TINKER, TINKER, HATCHET])
    assert MakeTools()._decide_craft_target(ctx) == "Pickaxe"


def test_low_skill_missing_hatchet_crafts_hatchet():
    ctx = _make_ctx(tinker_skill=10.0, graphics=[TINKER, TINKER, PICKAXE])
    assert MakeTools()._decide_craft_target(ctx) == "Hatchet"


def test_no_spare_tinker_tool_still_bootstraps_tinker_tools_first():
    """With only one (or zero) tinker tools, replicate a spare before anything
    else — execute() consumes tools[0] and tinker tools wear out too."""
    ctx = _make_ctx(tinker_skill=10.0, graphics=[TINKER])  # no pickaxe either
    assert MakeTools()._decide_craft_target(ctx) == "Tinker's Tools"


def test_mid_skill_missing_pickaxe_still_crafts_pickaxe():
    """Behavior preserved for the previously-correct mid-skill branch."""
    ctx = _make_ctx(tinker_skill=40.0, graphics=[TINKER, TINKER, HATCHET])
    assert MakeTools()._decide_craft_target(ctx) == "Pickaxe"


def test_all_tools_present_below_target_trains_on_pickaxe():
    ctx = _make_ctx(tinker_skill=40.0,
                    graphics=[TINKER, TINKER, PICKAXE, HATCHET])
    assert MakeTools()._decide_craft_target(ctx) == "Pickaxe"


def test_all_tools_present_at_target_stops():
    ctx = _make_ctx(tinker_skill=80.0,
                    graphics=[TINKER, TINKER, PICKAXE, HATCHET])
    assert MakeTools()._decide_craft_target(ctx) is None
