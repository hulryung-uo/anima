"""Action primitives — composable async packet sequences.

Each primitive sends packet(s), waits for the expected response, and
returns ActionResult (or its module-specific result type). Primitives
are stateless and know nothing about mining or crafting — they just
execute packet sequences with proper waiting.

This package is also the SINGLE IMPORT FAÇADE over both primitive
hierarchies (the older ``anima.action`` and the newer ``anima.actions``):

    from anima.actions import go_to, use_on_target, cast_spell

See ``docs/actions.md`` for the authoritative catalog (signatures,
preconditions, failure modes). Exports resolve lazily (PEP 562) to
avoid import cycles — many planner modules import action modules from
inside functions.
"""

from anima.actions.result import ActionResult

# name → defining module. Keep in sync with docs/actions.md.
_EXPORTS = {
    # movement (anima/action/movement.py)
    "go_to": "anima.action.movement",
    "wander_action": "anima.action.movement",
    # interaction (anima/action/interaction.py)
    "use_item": "anima.action.interaction",
    "double_click": "anima.action.interaction",
    "drag_to_ground": "anima.action.interaction",
    "drag_to_container": "anima.action.interaction",
    "move_item_on_ground": "anima.action.interaction",
    # speech (anima/action/speech.py)
    "respond_to_speech": "anima.action.speech",
    # targeting
    "use_on_target": "anima.actions.target",
    "use_on_object": "anima.actions.target",
    "wait_for_target": "anima.actions.target",
    "target_tile": "anima.actions.target",
    "target_object": "anima.actions.target",
    "cancel_target": "anima.actions.target",
    # inventory
    "find_in_backpack": "anima.actions.inventory",
    "count_items": "anima.actions.inventory",
    "drag_drop": "anima.actions.inventory",
    # vendor
    "request_context_menu": "anima.actions.vendor",
    "select_context_menu_entry": "anima.actions.vendor",
    "wait_for_buy_list": "anima.actions.vendor",
    "wait_for_sell_list": "anima.actions.vendor",
    # gumps
    "wait_for_gump": "anima.actions.gump",
    "click_gump_button": "anima.actions.gump",
    "craft_via_gump": "anima.actions.gump",
    # journal
    "wait_for_journal": "anima.actions.journal",
    # skills
    "use_skill": "anima.actions.skills",
    "use_skill_on": "anima.actions.skills",
    "meditate": "anima.actions.skills",
    # spells
    "cast_spell": "anima.actions.spells",
    "CastResult": "anima.actions.spells",
    # equip
    "equip_item": "anima.actions.equip",
    "equip_weapon_from_pack": "anima.actions.equip",
    # The shield-equip primitive (Parrying stream) is just as public as the
    # weapon one and is imported by combat_loop — it must be reachable through
    # the façade too, or `from anima.actions import equip_shield_from_pack`
    # (the documented single-import-façade pattern) raises AttributeError.
    "equip_shield_from_pack": "anima.actions.equip",
    # loot (anima/actions/loot.py) — a whole primitive module that was never
    # wired into the façade despite being a first-class action used by
    # combat_loop / planner / melee.
    "find_corpses": "anima.actions.loot",
    "loot_corpse": "anima.actions.loot",
}

__all__ = ["ActionResult", *_EXPORTS]


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'anima.actions' has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(target), name)
