"""State encoder — converts BrainContext into a discrete state key for Q-table."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

# Region size for location value map (32x32 tiles per region)
REGION_SIZE = 32


def encode_state(ctx: BrainContext) -> str:
    """Encode current game state into a string key for Q-table lookup.

    State components:
    - location_type: what kind of area we're in
    - has_players: whether players are nearby
    - has_enemies: whether hostile mobs are nearby
    - hp_level: health status
    - inventory_state: what's in our backpack
    """
    parts = [
        _location_type(ctx),
        _player_presence(ctx),
        _enemy_presence(ctx),
        _hp_level(ctx),
        _inventory_state(ctx),
    ]
    return "|".join(parts)


def region_coords(x: int, y: int) -> tuple[int, int]:
    """Convert world coordinates to region coordinates."""
    return x // REGION_SIZE, y // REGION_SIZE


def _location_type(ctx: BrainContext) -> str:
    """Infer location type from position and nearby objects."""
    ss = ctx.perception.self_state
    world = ctx.perception.world
    nearby = world.nearby_items(ss.x, ss.y, distance=10)
    nearby_graphics = {it.graphic for it in nearby}

    # Forge/anvil = smithy
    forge_graphics = {0x0FB1, 0x197A, 0x197E, 0x19A9, 0x0DE3, 0x0DE6}
    anvil_graphics = {0x0FAF, 0x0FB0, 0x2DD5, 0x2DD6}
    if nearby_graphics & (forge_graphics | anvil_graphics):
        return "smithy"

    # Water tiles nearby = waterside
    water_graphics = {0x1797, 0x1798, 0x1799, 0x179A, 0x346E}
    if nearby_graphics & water_graphics:
        return "water"

    # Check for NPCs (vendors, bankers, etc.)
    nearby_mobs = world.nearby_mobiles(ss.x, ss.y, distance=10)
    has_invulnerable = any(
        m.notoriety is not None and m.notoriety.value == 7
        for m in nearby_mobs
    )
    if has_invulnerable:
        return "town"

    # Default
    return "field"


def _player_presence(ctx: BrainContext) -> str:
    ss = ctx.perception.self_state
    nearby = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=18)
    has_players = any(
        m.notoriety is not None and m.notoriety.value <= 6
        for m in nearby
    )
    return "players" if has_players else "alone"


def _enemy_presence(ctx: BrainContext) -> str:
    ss = ctx.perception.self_state
    nearby = ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=18)
    has_enemies = any(
        m.notoriety is not None and m.notoriety.value in (3, 5, 6)
        for m in nearby
    )
    return "enemies" if has_enemies else "safe"


def _hp_level(ctx: BrainContext) -> str:
    hp = ctx.perception.self_state.hp_percent
    if hp >= 90:
        return "full"
    if hp >= 50:
        return "healthy"
    if hp >= 25:
        return "wounded"
    return "critical"


def _inventory_state(ctx: BrainContext) -> str:
    """Rough description of what's in the backpack."""
    ss = ctx.perception.self_state
    world = ctx.perception.world

    backpack = ss.equipment.get(0x15)
    if not backpack:
        return "no_pack"

    items = [it for it in world.items.values() if it.container == backpack]
    if not items:
        return "empty"

    graphics = {it.graphic for it in items}

    # Check for notable item categories
    ore_graphics = {0x19B7, 0x19B8, 0x19B9, 0x19BA}
    ingot_graphics = {0x1BF2, 0x1BEF, 0x1BF0, 0x1BF1}
    log_graphics = {0x1BDD, 0x1BE0}
    bandage_graphics = {0x0E21}

    tags = []
    if graphics & ore_graphics:
        tags.append("ore")
    if graphics & ingot_graphics:
        tags.append("ingots")
    if graphics & log_graphics:
        tags.append("logs")
    if graphics & bandage_graphics:
        tags.append("bandages")

    if ss.weight > 0 and ss.weight_max > 0 and ss.weight / ss.weight_max > 0.8:
        tags.append("heavy")

    return "+".join(tags) if tags else "misc"
