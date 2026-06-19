"""Land tile ID masking — anima/map.py must strip the high 2 bits (0x3FFF).

Mirrors ClassicUO's Chunk.cs: `tileID = cells[pos].TileID & 0x3FFF`. Land
tiledata only has 16384 (0x4000) entries, so an unmasked tile id with bit
0x4000/0x8000 set would miss the flag table and an impassable land tile
(e.g. water / void / mountain) would wrongly read as passable.
"""

from __future__ import annotations

import struct

from anima.map import (
    FLAG_IMPASSABLE,
    MAP_BLOCK_BYTES,
    MapReader,
)


class _FakeUop:
    """Returns a single 8x8 land chunk whose cell 0 has a high bit set."""

    def __init__(self, raw_tile_id: int, z: int = 5) -> None:
        # One UOP chunk holds BLOCKS_PER_UOP_CHUNK blocks; we only need block 0.
        buf = bytearray(MAP_BLOCK_BYTES)
        # 4-byte block header (unused) then 64 cells of (u16 graphic, s8 z).
        struct.pack_into("<H", buf, 4, raw_tile_id)
        struct.pack_into("<b", buf, 4 + 2, z)
        self._buf = bytes(buf)

    def get_by_pattern(self, pattern: str, index: int) -> bytes:
        return self._buf


def _make_reader(raw_tile_id: int, base_graphic: int) -> MapReader:
    reader = MapReader(resource_dir=".", data_dir=".")
    reader._uop = _FakeUop(raw_tile_id)  # type: ignore[assignment]
    # Only the masked graphic is a valid tiledata key; mark it impassable.
    reader._land_flags = {str(base_graphic): FLAG_IMPASSABLE}
    reader._item_data = {}
    # Isolate from real .mul resources: this test only exercises land-tile
    # masking, so return no statics rather than reading staidx0.mul off disk.
    reader._load_statics_block = lambda bx, by: []  # type: ignore[assignment]
    return reader


def test_land_tile_id_high_bits_are_masked():
    base_graphic = 0x0010  # e.g. some land tile within the 0x3FFF range
    raw = 0x4000 | base_graphic  # high bit set as occurs in real map data

    reader = _make_reader(raw, base_graphic)
    tile = reader.get_tile(0, 0)

    # Graphic must be the masked 14-bit value, matching ClassicUO.
    assert tile.land.graphic == base_graphic
    # And the flag lookup must therefore succeed: impassable land stays blocked.
    assert tile.land.flags & FLAG_IMPASSABLE
    assert tile.land.impassable
    assert tile.passable is False
    assert tile.walkable is False


def test_unmasked_would_be_walkable_regression():
    """Without the mask, the flag lookup misses and the tile reads walkable.

    This locks in the regression: the raw (unmasked) key is not in the flag
    table, so a buggy reader returns flags=0 and the impassable tile would be
    treated as passable.
    """
    base_graphic = 0x0010
    raw = 0x8000 | base_graphic

    reader = _make_reader(raw, base_graphic)
    tile = reader.get_tile(0, 0)

    # Correct (masked) behaviour:
    assert tile.land.graphic == base_graphic
    assert tile.passable is False

    # Demonstrate that the unmasked key genuinely would have missed the table.
    assert str(raw) not in reader._land_flags  # type: ignore[operator]
    assert reader._land_flags.get(str(raw), 0) == 0  # type: ignore[union-attr]


def test_low_id_unaffected():
    """A normal in-range tile id passes through unchanged."""
    base_graphic = 0x00C4
    reader = _make_reader(base_graphic, base_graphic)
    tile = reader.get_tile(0, 0)
    assert tile.land.graphic == base_graphic
    assert tile.land.impassable
