#!/usr/bin/env python3
"""Walk test — diagnose movement issues step by step.

Connects to server, tries walking in each direction, and reports
exactly what happens: map data, server response, position change.

Usage:
    uv run python tools/walk_test.py
    uv run python tools/walk_test.py --target 1600 1595   # walk to specific location
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import time

sys.path.insert(0, ".")

from anima.client.connection import UoConnection
from anima.client.handler import PacketHandler
from anima.client.packets import (
    build_double_click,
    build_status_request,
    build_walk_request,
)
from anima.config import load_config
from anima.map import MapReader
from anima.pathfinding import DIRECTION_DELTAS, direction_to, find_path
from anima.perception import Perception
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager
from anima.main import recv_loop

DIR_NAMES = {0: "N", 1: "NE", 2: "E", 3: "SE", 4: "S", 5: "SW", 6: "W", 7: "NW"}


def print_tile_info(mr: MapReader, x: int, y: int, z: int) -> None:
    """Print detailed tile info at (x, y)."""
    tile = mr.get_tile(x, y)
    can_walk_basic = tile.walkable
    can_walk_z, new_z = tile.walkable_z(z)

    statics_info = []
    for s in tile.statics:
        flags = []
        if s.impassable:
            flags.append("IMP")
        if s.surface:
            flags.append("SURF")
        statics_info.append(
            f"    {s.name or '?'} z={s.z} h={s.height} "
            f"flags={'|'.join(flags) or 'none'}"
        )

    print(f"  Tile ({x},{y}): land_z={tile.land.z} "
          f"walkable={can_walk_basic} walkable_z({z})={can_walk_z}→z={new_z}")
    if statics_info:
        for si in statics_info:
            print(si)


async def test_single_walk(
    conn: UoConnection,
    walker: WalkerManager,
    perception: Perception,
    direction: int,
) -> dict:
    """Send one walk packet and wait for result."""
    ss = perception.self_state
    old_x, old_y, old_z = ss.x, ss.y, ss.z

    dx, dy = DIRECTION_DELTAS[direction]
    target_x, target_y = old_x + dx, old_y + dy

    # Wait until can walk
    for _ in range(20):
        if walker.can_walk():
            break
        await asyncio.sleep(0.1)
    else:
        return {"error": "can_walk timeout", "direction": direction}

    walker._pending_step_tile = (target_x, target_y)
    seq = walker.next_sequence()
    fastwalk = walker.pop_fast_walk_key()
    pkt = build_walk_request(direction, seq, fastwalk)

    # Log packet bytes
    pkt_hex = " ".join(f"{b:02X}" for b in pkt)

    await conn.send_packet(pkt)
    walker.steps_count += 1
    walker.last_step_time = asyncio.get_event_loop().time() * 1000 + 500

    # Wait for server response
    await asyncio.sleep(0.6)

    new_x, new_y, new_z = ss.x, ss.y, ss.z
    moved = (new_x != old_x or new_y != old_y)

    return {
        "direction": direction,
        "dir_name": DIR_NAMES[direction],
        "target": (target_x, target_y),
        "seq": seq,
        "fastwalk": fastwalk,
        "packet": pkt_hex,
        "moved": moved,
        "old_pos": (old_x, old_y, old_z),
        "new_pos": (new_x, new_y, new_z),
        "steps_count": walker.steps_count,
        "consecutive_denials": walker.consecutive_denials,
    }


async def run_test(target_x: int | None = None, target_y: int | None = None):
    cfg = load_config()
    conn = UoConnection(timeout=cfg.client.connection_timeout)
    perception = Perception(player_serial=0)
    walker = WalkerManager(perception.self_state, perception.events)
    pkt_handler = PacketHandler()
    register_handlers(pkt_handler, perception, walker)

    print(f"Connecting to {cfg.server.host}:{cfg.server.port}...")
    result = await conn.login(
        cfg.server.host, cfg.server.port,
        cfg.account.username, cfg.account.password,
        character_name=cfg.character.name,
        character_persona=cfg.character.persona,
        character_city=cfg.character.city_index,
        packet_handler=pkt_handler,
        perception=perception,
    )
    perception.self_state.serial = result.serial
    print(f"Logged in: serial=0x{result.serial:08X} pos=({result.x},{result.y},{result.z})")

    # Start recv loop
    recv_task = asyncio.create_task(recv_loop(conn, pkt_handler))

    # Wait for world to load
    await asyncio.sleep(2.0)
    await conn.send_packet(build_double_click(result.serial))
    await asyncio.sleep(1.0)

    ss = perception.self_state
    print(f"\n=== CURRENT STATE ===")
    print(f"Position: ({ss.x}, {ss.y}, {ss.z})")
    print(f"Direction: {ss.direction}")
    print(f"Walker: seq={walker.walk_sequence} steps={walker.steps_count}")
    print(f"Fastwalk keys: {walker.fast_walk_keys}")

    # Load map
    from pathlib import Path
    mr = MapReader(Path(cfg.map.resource_dir).expanduser())

    print(f"\n=== MAP DATA AT CURRENT POSITION ===")
    print_tile_info(mr, ss.x, ss.y, ss.z)

    print(f"\n=== ADJACENT TILES (map data) ===")
    for d in range(8):
        dx, dy = DIRECTION_DELTAS[d]
        nx, ny = ss.x + dx, ss.y + dy
        tile = mr.get_tile(nx, ny)
        can, nz = tile.walkable_z(ss.z)
        marker = "OK" if can else "##"
        static_names = [s.name for s in tile.statics if s.name][:2]
        print(f"  {DIR_NAMES[d]:2s} ({nx},{ny}): {marker} z→{nz} "
              f"statics={static_names}")

    print(f"\n=== WALK TEST: ALL 8 DIRECTIONS ===")
    for d in range(8):
        result_d = await test_single_walk(conn, walker, perception, d)
        status = "MOVED" if result_d["moved"] else "DENIED"
        print(
            f"  {result_d['dir_name']:2s} → ({result_d['target'][0]},{result_d['target'][1]}): "
            f"{status} | seq={result_d['seq']} "
            f"pos=({ss.x},{ss.y},{ss.z}) "
            f"denials={result_d['consecutive_denials']}"
        )
        # Reset denials for clean test
        walker.consecutive_denials = 0

    if target_x is not None and target_y is not None:
        print(f"\n=== PATHFIND TEST: ({ss.x},{ss.y}) → ({target_x},{target_y}) ===")

        denied = set(walker.denied_tiles.keys())
        print(f"Denied tiles in cache: {len(denied)}")

        path = find_path(mr, ss.x, ss.y, target_x, target_y,
                         max_steps=200, current_z=ss.z)
        if path:
            print(f"Path found: {len(path)} steps")
            print(f"First 10 waypoints: {path[:10]}")

            print(f"\n=== WALKING PATH (first 10 steps) ===")
            for i, (wx, wy) in enumerate(path[:10]):
                d = direction_to(ss.x, ss.y, wx, wy)
                result_w = await test_single_walk(
                    conn, walker, perception, d,
                )
                status = "MOVED" if result_w["moved"] else "DENIED"
                print(
                    f"  Step {i+1}: {DIR_NAMES[d]} → ({wx},{wy}): "
                    f"{status} | actual=({ss.x},{ss.y},{ss.z})"
                )
                if not result_w["moved"]:
                    print(f"    Map says:")
                    print_tile_info(mr, wx, wy, ss.z)
                    print(f"    → Server denied! Map/server mismatch.")
                    break
                walker.consecutive_denials = 0
        else:
            print("No path found!")
            # Try without denied tiles
            path2 = find_path(mr, ss.x, ss.y, target_x, target_y,
                              max_steps=300)
            if path2:
                print(f"Path found WITHOUT denied tiles: {len(path2)} steps")
            else:
                print("No path even without denied tiles!")

    # Cleanup
    recv_task.cancel()
    print("\n=== DONE ===")


def main():
    parser = argparse.ArgumentParser(description="Walk diagnostic test")
    parser.add_argument("--target", nargs=2, type=int, metavar=("X", "Y"),
                        help="Target coordinates to pathfind to")
    args = parser.parse_args()

    target_x = args.target[0] if args.target else None
    target_y = args.target[1] if args.target else None

    asyncio.run(run_test(target_x, target_y))


if __name__ == "__main__":
    main()
