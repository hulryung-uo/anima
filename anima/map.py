"""UO map reader — land tiles, statics, and walkability queries."""

from __future__ import annotations

import struct
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from anima.uop import UopReader

# Tiledata flags
FLAG_IMPASSABLE = 0x00000040
FLAG_SURFACE = 0x00000200
FLAG_BRIDGE = 0x00000400
FLAG_WALL = 0x00000004
FLAG_WET = 0x00000008  # water
FLAG_DOOR = 0x20000000
FLAG_FOLIAGE = 0x01000000

# Map0 dimensions (Felucca / Trammel)
MAP_WIDTH = 7168
MAP_HEIGHT = 4096
BLOCK_SIZE = 8
BLOCKS_PER_UOP_CHUNK = 4096
MAP_BLOCK_BYTES = 196  # 4 header + 64 × 3 cells

# Block-cache cap. The agent roams across hundreds of thousands of distinct
# 8×8 blocks over a soak; with only ``cache[key]=...`` writes and no eviction
# both _land_cache and _statics_cache grew without bound. Movement has strong
# spatial locality (recently-visited blocks recur), so a generous LRU cap keeps
# the hot working set resident while bounding resident memory. ~4096 blocks ≈ a
# 512×512-tile window, far larger than any single A* expansion or render_area.
MAP_BLOCK_CACHE_MAX = 4096


@dataclass(slots=True)
class LandTile:
    graphic: int
    z: int
    flags: int = 0

    @property
    def impassable(self) -> bool:
        return bool(self.flags & FLAG_IMPASSABLE)


@dataclass(slots=True)
class StaticItem:
    graphic: int
    x: int  # 0-7 within block
    y: int  # 0-7 within block
    z: int
    hue: int
    flags: int = 0
    name: str = ""
    height: int = 0  # from tiledata

    @property
    def impassable(self) -> bool:
        return bool(self.flags & FLAG_IMPASSABLE)

    @property
    def surface(self) -> bool:
        return bool(self.flags & (FLAG_SURFACE | FLAG_BRIDGE))

    @property
    def top_z(self) -> int:
        """Z coordinate of the top of this item."""
        return self.z + self.height


@dataclass(slots=True)
class TileInfo:
    x: int
    y: int
    land: LandTile
    statics: list[StaticItem]

    @property
    def passable(self) -> bool:
        if self.land.impassable:
            return False
        for s in self.statics:
            if s.impassable and not s.surface:
                return False
        return True

    @property
    def walkable(self) -> bool:
        """Check if this tile can be walked on (ignoring Z)."""
        if self.land.impassable:
            return False
        for s in self.statics:
            if s.impassable and not s.surface:
                return False
        return True

    def walkable_z(self, current_z: int) -> tuple[bool, int]:
        """Z-aware walkability check following ClassicUO's algorithm.

        Args:
            current_z: The Z coordinate of the entity trying to walk here.

        Returns:
            (can_walk, new_z) — whether the tile is walkable from current_z,
            and what Z the entity would be standing at after stepping.
        """
        max_step = 16  # DEFAULT_BLOCK_HEIGHT from ClassicUO
        char_height = 16  # DEFAULT_CHARACTER_HEIGHT

        # Start with land as the best standing surface (if walkable)
        land_ok = not self.land.impassable
        best_z = self.land.z if land_ok else current_z
        has_surface = land_ok  # track whether we found any walkable surface

        for s in self.statics:
            standing_z = s.z + (s.height // 2 if s.flags & FLAG_BRIDGE else s.height)
            top_z = s.z + s.height

            if s.impassable and not s.surface:
                # Blocker — check if it overlaps with our body at current_z.
                # Our body occupies [current_z, current_z + char_height).
                # A great many impassable, non-surface statics (lava, statues,
                # webs, grapevines: ~1000 graphics in tiledata) have height==0,
                # so top_z == s.z. A naive `top_z > current_z` test then lets a
                # blocker sitting flush at the agent's feet (s.z == current_z)
                # read as walkable, and A* routes a path straight through it.
                # A zero-height blocker still occupies its own z-plane, so its
                # effective top is s.z + max(height, 1).
                blocker_top = s.z + max(s.height, 1)
                if blocker_top > current_z and s.z < current_z + char_height:
                    return False, current_z
            elif s.surface:
                # Static surface (e.g. cave floor) — walkable even if land is void
                if abs(standing_z - current_z) <= max_step:
                    if not has_surface or abs(standing_z - current_z) < abs(best_z - current_z):
                        best_z = standing_z
                        has_surface = True

        if not has_surface:
            return False, current_z

        # Final check: can we step from current_z to best_z?
        if abs(best_z - current_z) > max_step:
            return False, current_z

        return True, best_z


class MapReader:
    """On-demand UO map reader with block caching."""

    def __init__(self, resource_dir: str | Path, data_dir: str | Path | None = None) -> None:
        self._resource = Path(resource_dir)
        self._data = Path(data_dir) if data_dir else Path(__file__).parent.parent / "data"

        # UOP map reader (lazy)
        self._uop: UopReader | None = None
        self._uop_pattern = "build/map0legacymul/{0:08d}.dat"

        # Statics files (lazy)
        self._staidx_data: bytes | None = None
        self._statics_data: bytes | None = None

        # Tiledata (lazy)
        self._land_flags: dict[str, int] | None = None
        self._item_data: dict[str, dict] | None = None

        # Block cache (LRU-bounded — see MAP_BLOCK_CACHE_MAX). OrderedDict keeps
        # insertion/access order so the least-recently-used block is evicted once
        # the cap is exceeded. block_key = (bx << 16) | by.
        self._land_cache: OrderedDict[int, list[tuple[int, int]]] = OrderedDict()
        self._statics_cache: OrderedDict[int, list[StaticItem]] = OrderedDict()

    @staticmethod
    def _cache_get(cache: OrderedDict, key: int):
        """LRU read: return the cached value (marking it recently used) or None."""
        value = cache.get(key)
        if value is not None or key in cache:
            # Refresh recency even for falsy values (empty statics list, etc.).
            cache.move_to_end(key)
            return value
        return None

    @staticmethod
    def _cache_put(cache: OrderedDict, key: int, value) -> None:
        """LRU write: insert as most-recent, evicting the oldest past the cap."""
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > MAP_BLOCK_CACHE_MAX:
            cache.popitem(last=False)  # drop least-recently-used

    def _ensure_uop(self) -> UopReader:
        if self._uop is None:
            path = self._resource / "map0LegacyMUL.uop"
            self._uop = UopReader(path)
        return self._uop

    def _ensure_statics(self) -> tuple[bytes, bytes]:
        if self._staidx_data is None:
            self._staidx_data = (self._resource / "staidx0.mul").read_bytes()
            self._statics_data = (self._resource / "statics0.mul").read_bytes()
        return self._staidx_data, self._statics_data

    def _ensure_tiledata(self) -> None:
        if self._land_flags is None:
            import json
            land_path = self._data / "tiledata_land.json"
            if land_path.exists():
                with open(land_path) as f:
                    self._land_flags = json.load(f)
            else:
                self._land_flags = {}

            items_path = self._data / "tiledata_items.json"
            if items_path.exists():
                with open(items_path) as f:
                    self._item_data = json.load(f)
            else:
                self._item_data = {}

    def _get_land_flags(self, graphic: int) -> int:
        self._ensure_tiledata()
        assert self._land_flags is not None
        return self._land_flags.get(str(graphic), 0)

    def _get_item_flags(self, graphic: int) -> int:
        self._ensure_tiledata()
        assert self._item_data is not None
        entry = self._item_data.get(str(graphic))
        # A regenerated/partial tiledata row may omit "flags"; default to 0
        # rather than KeyError (mirrors anima.data's missing-key tolerance).
        return entry.get("flags", 0) if entry else 0

    def _get_item_height(self, graphic: int) -> int:
        self._ensure_tiledata()
        assert self._item_data is not None
        entry = self._item_data.get(str(graphic))
        return entry.get("height", 0) if entry else 0

    def _get_item_name(self, graphic: int) -> str:
        self._ensure_tiledata()
        assert self._item_data is not None
        entry = self._item_data.get(str(graphic))
        if not entry:
            return ""
        # Resolve UO ``%plural/singular%`` format codes the way ClassicUO does
        # (anima.data._plural_adjust). The old ``.replace("%s%", "").replace(
        # "%", "")`` left the stranded singular half of every slash form behind
        # ("rub%ies/y%" -> "rubies/y", "pil%es/e% of hides" -> "piles/e of
        # hides"), and ``entry["name"]`` KeyError'd on a partial row. This
        # name flows into StaticItem.name and render_area's LLM map context.
        from anima.data import _plural_adjust

        return _plural_adjust(entry.get("name", "") or "").strip()

    def _load_land_block(self, bx: int, by: int) -> list[tuple[int, int]]:
        """Load a single 8×8 land block. Returns 64 (graphic, z) tuples."""
        blocks_x = MAP_WIDTH // BLOCK_SIZE
        block_num = bx * (MAP_HEIGHT // BLOCK_SIZE) + by

        key = (bx << 16) | by
        cached = self._cache_get(self._land_cache, key)
        if cached is not None:
            return cached

        uop = self._ensure_uop()
        chunk_idx = block_num // BLOCKS_PER_UOP_CHUNK
        block_in_chunk = block_num % BLOCKS_PER_UOP_CHUNK

        chunk_data = uop.get_by_pattern(self._uop_pattern, chunk_idx)
        if chunk_data is None:
            cells = [(0, 0)] * 64
            self._cache_put(self._land_cache, key, cells)
            return cells

        offset = block_in_chunk * MAP_BLOCK_BYTES + 4  # skip 4-byte header
        cells = []
        for i in range(64):
            pos = offset + i * 3
            if pos + 3 <= len(chunk_data):
                # Land tile IDs are 14-bit; the top 2 bits are not part of the
                # graphic. ClassicUO masks with 0x3FFF (Chunk.cs: TileID & 0x3FFF)
                # before any tiledata/flag lookup. Without this, cells with high
                # bits set miss the flag table and impassable land reads as walkable.
                tile_id = struct.unpack_from("<H", chunk_data, pos)[0] & 0x3FFF
                z = struct.unpack_from("<b", chunk_data, pos + 2)[0]
                cells.append((tile_id, z))
            else:
                cells.append((0, 0))

        self._cache_put(self._land_cache, key, cells)
        return cells

    def _load_statics_block(self, bx: int, by: int) -> list[StaticItem]:
        """Load statics for an 8×8 block."""
        key = (bx << 16) | by
        cached = self._cache_get(self._statics_cache, key)
        if cached is not None:
            return cached

        staidx, statics = self._ensure_statics()
        blocks_y = MAP_HEIGHT // BLOCK_SIZE
        block_num = bx * blocks_y + by

        idx_offset = block_num * 12
        if idx_offset + 12 > len(staidx):
            self._cache_put(self._statics_cache, key, [])
            return []

        data_offset = struct.unpack_from("<I", staidx, idx_offset)[0]
        data_length = struct.unpack_from("<I", staidx, idx_offset + 4)[0]

        if data_offset == 0xFFFFFFFF or data_length == 0:
            self._cache_put(self._statics_cache, key, [])
            return []

        items: list[StaticItem] = []
        pos = data_offset
        end = data_offset + data_length
        while pos + 7 <= end and pos + 7 <= len(statics):
            graphic = struct.unpack_from("<H", statics, pos)[0]
            x_off = statics[pos + 2]
            y_off = statics[pos + 3]
            z = struct.unpack_from("<b", statics, pos + 4)[0]
            hue = struct.unpack_from("<H", statics, pos + 5)[0]
            pos += 7

            flags = self._get_item_flags(graphic)
            name = self._get_item_name(graphic)
            height = self._get_item_height(graphic)
            items.append(StaticItem(
                graphic=graphic, x=x_off, y=y_off, z=z,
                hue=hue, flags=flags, name=name, height=height,
            ))

        self._cache_put(self._statics_cache, key, items)
        return items

    def get_tile(self, x: int, y: int) -> TileInfo:
        """Get full tile info at world coordinates."""
        # Off-map coordinates have no land block. Python floor-division/modulo
        # do NOT clamp here: x=-1 yields bx=-1, cx=7, so block_num goes negative,
        # block_in_chunk wraps to a *real but wrong* block (e.g. -512 -> chunk -1,
        # block_in_chunk 3584) and the cache key ((bx<<16)|by) collides with
        # other negatives. The wrong block (or the empty graphic-0 fallback when
        # the chunk lookup misses) is NOT impassable, so the void reads as
        # WALKABLE. A* then happily expands nodes off the west/north edge of the
        # world and path_is_traversable would confirm a route that walks off the
        # map. UO has no tiles outside [0,MAP_WIDTH) x [0,MAP_HEIGHT); return an
        # impassable void tile so every map-edge query is correctly blocked.
        if x < 0 or y < 0 or x >= MAP_WIDTH or y >= MAP_HEIGHT:
            return TileInfo(
                x=x,
                y=y,
                land=LandTile(graphic=0, z=0, flags=FLAG_IMPASSABLE),
                statics=[],
            )

        bx = x // BLOCK_SIZE
        by = y // BLOCK_SIZE
        cx = x % BLOCK_SIZE
        cy = y % BLOCK_SIZE

        cells = self._load_land_block(bx, by)
        cell_idx = cy * BLOCK_SIZE + cx  # row-major within block
        graphic, z = cells[cell_idx]
        land_flags = self._get_land_flags(graphic)
        land = LandTile(graphic=graphic, z=z, flags=land_flags)

        block_statics = self._load_statics_block(bx, by)
        tile_statics = [s for s in block_statics if s.x == cx and s.y == cy]

        return TileInfo(x=x, y=y, land=land, statics=tile_statics)

    def render_area(
        self, cx: int, cy: int, radius: int = 10,
    ) -> str:
        """Render an ASCII map around (cx, cy).

        Legend: . = walkable, # = blocked, ~ = water, T = tree/foliage, + = door
        """
        lines = []
        for y in range(cy - radius, cy + radius + 1):
            row = []
            for x in range(cx - radius, cx + radius + 1):
                if x == cx and y == cy:
                    row.append("@")
                    continue
                tile = self.get_tile(x, y)
                ch = "."
                # Check land
                if tile.land.flags & FLAG_WET:
                    ch = "~"
                elif tile.land.impassable:
                    ch = "#"
                # Check statics (highest priority last)
                for s in tile.statics:
                    if s.flags & FLAG_DOOR:
                        ch = "+"
                    elif s.flags & FLAG_FOLIAGE:
                        ch = "T"
                    elif s.impassable and not s.surface:
                        ch = "#"
                    elif s.flags & FLAG_WET:
                        ch = "~"
                row.append(ch)
            lines.append("".join(row))
        return "\n".join(lines)
