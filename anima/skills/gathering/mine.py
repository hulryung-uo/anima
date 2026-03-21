"""Mining skill — use a pickaxe on mountain/cave tiles to gather ore.

ServUO mining targets land tiles and static tiles (not dynamic items).
The tile ID list matches m_MountainAndCaveTiles in Mining.cs.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from anima.client.packets import build_double_click, build_target_response
from anima.skills.base import Skill, SkillResult

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

logger = structlog.get_logger()

PICKAXE_GRAPHICS = {0x0E86, 0x0E85}
SHOVEL_GRAPHIC = 0x0F39

ORE_GRAPHICS = {0x19B7, 0x19B8, 0x19B9, 0x19BA}
MINING_SKILL_ID = 45

SEARCH_RADIUS = 2  # mining range is 2 tiles

# From ServUO Mining.cs m_MountainAndCaveTiles — land and static tile IDs
# fmt: off
MINEABLE_TILES: set[int] = {
    220, 221, 222, 223, 224, 225, 226, 227, 228, 229,
    230, 231, 236, 237, 238, 239, 240, 241, 242, 243,
    244, 245, 246, 247, 252, 253, 254, 255, 256, 257,
    258, 259, 260, 261, 262, 263, 268, 269, 270, 271,
    272, 273, 274, 275, 276, 277, 278, 279, 286, 287,
    288, 289, 290, 291, 292, 293, 294, 296, 297,
    321, 322, 323, 324, 467, 468, 469, 470, 471, 472,
    473, 474, 476, 477, 478, 479, 480, 481, 482, 483,
    484, 485, 486, 487, 492, 493, 494, 495, 543, 544,
    545, 546, 547, 548, 549, 550, 551, 552, 553, 554,
    555, 556, 557, 558, 559, 560, 561, 562, 563, 564,
    565, 566, 567, 568, 569, 570, 571, 572, 573, 574,
    575, 576, 577, 578, 579, 581, 582, 583, 584, 585,
    586, 587, 588, 589, 590, 591, 592, 593, 594, 595,
    596, 597, 598, 599, 600, 601, 610, 611, 612, 613,
    1010,
    1741, 1742, 1743, 1744, 1745, 1746, 1747, 1748, 1749,
    1750, 1751, 1752, 1753, 1754, 1755, 1756, 1757,
    1771, 1772, 1773, 1774, 1775, 1776, 1777, 1778, 1779,
    1780, 1781, 1782, 1783, 1784, 1785, 1786, 1787, 1788, 1789, 1790,
    1801, 1802, 1803, 1804, 1805, 1806, 1807, 1808, 1809,
    1811, 1812, 1813, 1814, 1815, 1816, 1817, 1818, 1819,
    1820, 1821, 1822, 1823, 1824,
    1831, 1832, 1833, 1834, 1835, 1836, 1837, 1838, 1839,
    1840, 1841, 1842, 1843, 1844, 1845, 1846, 1847, 1848, 1849,
    1850, 1851, 1852, 1853, 1854,
    1861, 1862, 1863, 1864, 1865, 1866, 1867, 1868, 1869,
    1870, 1871, 1872, 1873, 1874, 1875, 1876, 1877, 1878, 1879,
    1880, 1881, 1882, 1883, 1884,
    1981, 1982, 1983, 1984, 1985, 1986, 1987, 1988, 1989,
    1990, 1991, 1992, 1993, 1994, 1995, 1996, 1997, 1998, 1999,
    2000, 2001, 2002, 2003, 2004,
    2028, 2029, 2030, 2031, 2032, 2033,
    2100, 2101, 2102, 2103, 2104, 2105,
}
# fmt: on


def _find_mineable_tile(
    ctx: BrainContext,
) -> tuple[int, int, int, int, bool] | None:
    """Find a mineable tile within SEARCH_RADIUS.

    Checks map statics first, then land tiles.
    Returns (x, y, z, graphic, is_static) or None.
    """
    ss = ctx.perception.self_state
    sx, sy = ss.x, ss.y

    if ctx.map_reader is None:
        return None

    best = None
    best_dist = SEARCH_RADIUS + 1

    for dy in range(-SEARCH_RADIUS, SEARCH_RADIUS + 1):
        for dx in range(-SEARCH_RADIUS, SEARCH_RADIUS + 1):
            dist = max(abs(dx), abs(dy))
            if dist >= best_dist:
                continue
            tx, ty = sx + dx, sy + dy
            tile = ctx.map_reader.get_tile(tx, ty)

            # Check statics (cave walls, rock formations)
            for s in tile.statics:
                if s.graphic in MINEABLE_TILES:
                    best = (tx, ty, s.z, s.graphic, True)
                    best_dist = dist
                    break

            # Check land tile (mountain ground)
            if best_dist > dist and tile.land.graphic in MINEABLE_TILES:
                best = (tx, ty, tile.land.z, tile.land.graphic, False)
                best_dist = dist

    return best


class MineOre(Skill):
    """Mine rocks with a pickaxe to gather ore."""

    name = "mine_ore"
    category = "gathering"
    description = "Use a pickaxe on nearby mountain/cave tiles to mine ore."
    required_skill = (MINING_SKILL_ID, 0.0)

    async def can_execute(self, ctx: BrainContext) -> bool:
        ss = ctx.perception.self_state
        world = ctx.perception.world

        # Weight check
        if ss.weight_max > 0 and ss.weight >= ss.weight_max - 20:
            return False

        # Check for pickaxe/shovel in backpack
        backpack = ss.equipment.get(0x15)
        if not backpack:
            return False
        has_tool = any(
            it.graphic in PICKAXE_GRAPHICS or it.graphic == SHOVEL_GRAPHIC
            for it in world.items.values()
            if it.container == backpack
        )
        if not has_tool:
            return False

        # Check for mineable tiles via map reader
        return _find_mineable_tile(ctx) is not None

    async def execute(self, ctx: BrainContext) -> SkillResult:
        ss = ctx.perception.self_state
        world = ctx.perception.world
        start = time.monotonic()

        backpack = ss.equipment.get(0x15)
        tool = None
        for item in world.items.values():
            if item.container == backpack and (
                item.graphic in PICKAXE_GRAPHICS
                or item.graphic == SHOVEL_GRAPHIC
            ):
                tool = item
                break

        if not tool:
            return SkillResult(success=False, reward=-1.0, message="No mining tool")

        target = _find_mineable_tile(ctx)
        if not target:
            return SkillResult(success=False, reward=-1.0, message="No mineable tiles")

        tx, ty, tz, graphic, is_static = target

        # Count ore before
        ore_before = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in ORE_GRAPHICS
        )

        # Double-click pickaxe to enter targeting mode
        await ctx.conn.send_packet(build_double_click(tool.serial))
        await asyncio.sleep(0.5)

        # Target the tile (static or land — both use target_type=1)
        await ctx.conn.send_packet(build_target_response(
            target_type=1,
            cursor_id=0,
            x=tx,
            y=ty,
            z=tz,
            graphic=graphic if is_static else 0,
        ))

        # Wait for mining animation + result (~2 seconds)
        await asyncio.sleep(3.0)

        # Count ore after
        ore_after = sum(
            it.amount for it in world.items.values()
            if it.container == backpack and it.graphic in ORE_GRAPHICS
        )

        elapsed = (time.monotonic() - start) * 1000
        ore_gained = ore_after - ore_before

        if ore_gained > 0:
            reward = 5.0 + ore_gained
            logger.info("mine_success", ore=ore_gained, pos=f"({tx},{ty})")
            return SkillResult(
                success=True,
                reward=reward,
                message=f"Mined {ore_gained} ore",
                skill_gains=[(MINING_SKILL_ID, 0.1)],
                duration_ms=elapsed,
            )
        else:
            logger.info("mine_fail", pos=f"({tx},{ty})")
            return SkillResult(
                success=False,
                reward=-0.5,
                message="Failed to mine ore",
                duration_ms=elapsed,
            )
