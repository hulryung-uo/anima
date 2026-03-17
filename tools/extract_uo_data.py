"""Extract item names and cliloc strings from UO data files.

Usage:
    uv run python tools/extract_uo_data.py ~/dev/uo/uo-resource

Outputs:
    data/tiledata_items.json  — {graphic_id: {name, weight, height, flags}}
    data/cliloc.json          — {cliloc_number: text}
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path


def parse_tiledata(path: Path) -> tuple[dict[int, int], dict[int, dict]]:
    """Parse tiledata.mul (High Seas format, 8-byte flags).

    Layout:
      Land: 512 groups × (4-byte header + 32 × 30-byte tiles)
      Item: 2048 groups × (4-byte header + 32 × 41-byte tiles)
    """
    data = path.read_bytes()
    pos = 0

    # Parse land tiles: 512 groups
    LAND_GROUPS = 512
    LAND_TILE_SIZE = 30  # flags(8) + texture(2) + name(20)
    land: dict[int, int] = {}  # tile_id → flags
    land_id = 0
    for _ in range(LAND_GROUPS):
        pos += 4  # group header
        for _ in range(32):
            flags = struct.unpack_from("<Q", data, pos)[0]
            land[land_id] = flags
            pos += LAND_TILE_SIZE
            land_id += 1

    # Parse item tiles
    ITEM_TILE_SIZE = 41  # flags(8) + weight(1) + quality(1) + misc(2) + unk2(1) + quantity(1) + anim(2) + unk3(1) + hue(1) + stackoff(2) + height(1) + name(20)
    items: dict[int, dict] = {}
    graphic_id = 0

    while pos + 4 + 32 * ITEM_TILE_SIZE <= len(data):
        pos += 4  # group header
        for _ in range(32):
            flags = struct.unpack_from("<Q", data, pos)[0]
            weight = data[pos + 8]
            height = data[pos + 8 + 1 + 1 + 2 + 1 + 1 + 2 + 1 + 1 + 2]  # offset 20
            name_bytes = data[pos + 21 : pos + 41]
            name = name_bytes.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()

            if name:
                items[graphic_id] = {
                    "name": name,
                    "weight": weight,
                    "height": height,
                    "flags": flags,
                }

            pos += ITEM_TILE_SIZE
            graphic_id += 1

    return land, items


def parse_cliloc(path: Path) -> dict[int, str]:
    """Parse Cliloc.enu — localized string table.

    Layout: header(6) + entries(number:u32 + flag:u8 + length:u16 + text)
    """
    data = path.read_bytes()
    pos = 6  # skip header
    strings: dict[int, str] = {}

    while pos + 7 <= len(data):
        number = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        _flag = data[pos]
        pos += 1
        length = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if pos + length > len(data):
            break
        text = data[pos : pos + length].decode("utf-8", errors="replace")
        pos += length
        strings[number] = text

    return strings


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python tools/extract_uo_data.py <uo-resource-dir>")
        sys.exit(1)

    resource_dir = Path(sys.argv[1])
    output_dir = Path(__file__).parent.parent / "data"
    output_dir.mkdir(exist_ok=True)

    # Parse tiledata
    tiledata_path = resource_dir / "tiledata.mul"
    if tiledata_path.exists():
        print(f"Parsing {tiledata_path} ...")
        land, items = parse_tiledata(tiledata_path)

        out = output_dir / "tiledata_land.json"
        with open(out, "w") as f:
            json.dump(land, f, indent=1, ensure_ascii=False)
        print(f"  → {out} ({len(land)} land tiles)")

        out = output_dir / "tiledata_items.json"
        with open(out, "w") as f:
            json.dump(items, f, indent=1, ensure_ascii=False)
        print(f"  → {out} ({len(items)} items)")
    else:
        print(f"ERROR: {tiledata_path} not found")

    # Parse cliloc
    cliloc_path = resource_dir / "Cliloc.enu"
    if cliloc_path.exists():
        print(f"Parsing {cliloc_path} ...")
        strings = parse_cliloc(cliloc_path)
        out = output_dir / "cliloc.json"
        with open(out, "w") as f:
            json.dump(strings, f, indent=1, ensure_ascii=False)
        print(f"  → {out} ({len(strings)} strings)")
    else:
        print(f"ERROR: {cliloc_path} not found")


if __name__ == "__main__":
    main()
