"""UO map reader — land tiles, statics, and walkability queries."""

from __future__ import annotations

import struct
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

    @property
    def impassable(self) -> bool:
        return bool(self.flags & FLAG_IMPASSABLE)

    @property
    def surface(self) -> bool:
        return bool(self.flags & (FLAG_SURFACE | FLAG_BRIDGE))


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
        """Check if this tile can be walked on."""
        if self.land.impassable:
            return False
        for s in self.statics:
            if s.impassable and not s.surface:
                return False
        return True


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

        # Block cache
        self._land_cache: dict[int, list[tuple[int, int]]] = {}  # block_key → [(graphic, z) × 64]
        self._statics_cache: dict[int, list[StaticItem]] = {}

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
        return entry["flags"] if entry else 0

    def _get_item_name(self, graphic: int) -> str:
        self._ensure_tiledata()
        assert self._item_data is not None
        entry = self._item_data.get(str(graphic))
        if entry:
            return entry["name"].replace("%s%", "").replace("%", "").strip()
        return ""

    def _load_land_block(self, bx: int, by: int) -> list[tuple[int, int]]:
        """Load a single 8×8 land block. Returns 64 (graphic, z) tuples."""
        blocks_x = MAP_WIDTH // BLOCK_SIZE
        block_num = bx * (MAP_HEIGHT // BLOCK_SIZE) + by

        key = (bx << 16) | by
        if key in self._land_cache:
            return self._land_cache[key]

        uop = self._ensure_uop()
        chunk_idx = block_num // BLOCKS_PER_UOP_CHUNK
        block_in_chunk = block_num % BLOCKS_PER_UOP_CHUNK

        chunk_data = uop.get_by_pattern(self._uop_pattern, chunk_idx)
        if chunk_data is None:
            cells = [(0, 0)] * 64
            self._land_cache[key] = cells
            return cells

        offset = block_in_chunk * MAP_BLOCK_BYTES + 4  # skip 4-byte header
        cells = []
        for i in range(64):
            pos = offset + i * 3
            if pos + 3 <= len(chunk_data):
                tile_id = struct.unpack_from("<H", chunk_data, pos)[0]
                z = struct.unpack_from("<b", chunk_data, pos + 2)[0]
                cells.append((tile_id, z))
            else:
                cells.append((0, 0))

        self._land_cache[key] = cells
        return cells

    def _load_statics_block(self, bx: int, by: int) -> list[StaticItem]:
        """Load statics for an 8×8 block."""
        key = (bx << 16) | by
        if key in self._statics_cache:
            return self._statics_cache[key]

        staidx, statics = self._ensure_statics()
        blocks_y = MAP_HEIGHT // BLOCK_SIZE
        block_num = bx * blocks_y + by

        idx_offset = block_num * 12
        if idx_offset + 12 > len(staidx):
            self._statics_cache[key] = []
            return []

        data_offset = struct.unpack_from("<I", staidx, idx_offset)[0]
        data_length = struct.unpack_from("<I", staidx, idx_offset + 4)[0]

        if data_offset == 0xFFFFFFFF or data_length == 0:
            self._statics_cache[key] = []
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
            items.append(StaticItem(
                graphic=graphic, x=x_off, y=y_off, z=z,
                hue=hue, flags=flags, name=name,
            ))

        self._statics_cache[key] = items
        return items

    def get_tile(self, x: int, y: int) -> TileInfo:
        """Get full tile info at world coordinates."""
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
