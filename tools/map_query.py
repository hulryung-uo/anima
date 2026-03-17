"""Query UO map data and render ASCII map around coordinates.

Usage:
    uv run python tools/map_query.py 1601 1608         # Britain spawn
    uv run python tools/map_query.py 1601 1608 --radius 20
    uv run python tools/map_query.py 1601 1608 --detail  # show statics list
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from anima.map import MapReader


def main() -> None:
    parser = argparse.ArgumentParser(description="Query UO map data")
    parser.add_argument("x", type=int, help="X coordinate")
    parser.add_argument("y", type=int, help="Y coordinate")
    parser.add_argument("--radius", type=int, default=15, help="View radius")
    parser.add_argument(
        "--resource", default=str(Path.home() / "dev/uo/uo-resource"),
        help="UO resource directory",
    )
    parser.add_argument("--detail", action="store_true", help="Show tile details")
    args = parser.parse_args()

    print(f"Loading map data from {args.resource} ...")
    reader = MapReader(args.resource)

    if args.detail:
        tile = reader.get_tile(args.x, args.y)
        print(f"\nTile ({args.x}, {args.y}):")
        print(f"  Land: graphic=0x{tile.land.graphic:04X} z={tile.land.z}"
              f" flags=0x{tile.land.flags:08X}"
              f" walkable={'yes' if not tile.land.impassable else 'NO'}")
        if tile.statics:
            print(f"  Statics ({len(tile.statics)}):")
            for s in tile.statics:
                tags = []
                if s.impassable:
                    tags.append("IMPASSABLE")
                if s.surface:
                    tags.append("SURFACE")
                print(f"    {s.name or f'0x{s.graphic:04X}'}"
                      f" z={s.z} hue=0x{s.hue:04X}"
                      f" {' '.join(tags)}")
        else:
            print("  No statics")
        print(f"  Walkable: {'YES' if tile.walkable else 'NO'}")
        print()

    print(f"Map around ({args.x}, {args.y}), radius={args.radius}")
    print(f"Legend: @ = you, . = walk, # = block, ~ = water, T = tree, + = door\n")
    ascii_map = reader.render_area(args.x, args.y, args.radius)
    print(ascii_map)


if __name__ == "__main__":
    main()
