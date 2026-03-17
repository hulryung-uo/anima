"""Anima entry point — connect to servuo and walk around."""

from __future__ import annotations

import argparse
import asyncio
import random
import struct

import structlog

from anima.client.codec import PacketReader
from anima.client.connection import UoConnection
from anima.client.packets import build_ping, build_walk_request, get_packet_length

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
)
logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Simple WalkerManager (mirrors ClassicUO's WalkerManager)
# ---------------------------------------------------------------------------

MAX_STEP_COUNT = 5
MAX_FAST_WALK_STACK_SIZE = 5
TURN_DELAY_MS = 100
WALK_DELAY_MS = 400
RUN_DELAY_MS = 200


class WalkerManager:
    def __init__(self) -> None:
        self.walk_sequence: int = 0
        self.steps_count: int = 0
        self.walking_failed: bool = False
        self.last_step_time: float = 0.0
        self.fast_walk_keys: list[int] = [0] * MAX_FAST_WALK_STACK_SIZE
        self.player_x: int = 0
        self.player_y: int = 0
        self.player_z: int = 0
        self.player_direction: int = 0

    def reset(self) -> None:
        self.steps_count = 0
        self.walk_sequence = 0
        self.walking_failed = False
        self.last_step_time = 0.0

    def set_fast_walk_keys(self, keys: list[int]) -> None:
        """Server sent 0xBF subcmd 0x01 — replace all keys."""
        for i in range(min(len(keys), MAX_FAST_WALK_STACK_SIZE)):
            self.fast_walk_keys[i] = keys[i]

    def add_fast_walk_key(self, key: int) -> None:
        """Server sent 0xBF subcmd 0x02 — add one key."""
        for i in range(MAX_FAST_WALK_STACK_SIZE):
            if self.fast_walk_keys[i] == 0:
                self.fast_walk_keys[i] = key
                break

    def pop_fast_walk_key(self) -> int:
        """Get and consume next fastwalk key (0 if none available)."""
        for i in range(MAX_FAST_WALK_STACK_SIZE):
            key = self.fast_walk_keys[i]
            if key != 0:
                self.fast_walk_keys[i] = 0
                return key
        return 0

    def next_sequence(self) -> int:
        seq = self.walk_sequence
        if self.walk_sequence == 0xFF:
            self.walk_sequence = 1
        else:
            self.walk_sequence += 1
        return seq

    def confirm_walk(self, seq: int) -> None:
        if self.steps_count > 0:
            self.steps_count -= 1

    def deny_walk(self, seq: int, x: int, y: int, z: int) -> None:
        self.steps_count = 0
        self.player_x = x
        self.player_y = y
        self.player_z = z
        self.walking_failed = False  # allow retry

    def can_walk(self) -> bool:
        now = asyncio.get_event_loop().time() * 1000
        return (
            not self.walking_failed
            and self.steps_count < MAX_STEP_COUNT
            and now >= self.last_step_time
        )


walker = WalkerManager()


# ---------------------------------------------------------------------------
# Packet receive loop
# ---------------------------------------------------------------------------

async def recv_loop(conn: UoConnection) -> None:
    """Receive and process all game packets."""
    while conn.connected:
        try:
            packet_id, data = await conn.recv_packet(timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except (ConnectionError, EOFError):
            logger.error("connection_lost")
            break

        if packet_id == 0xBF and len(data) >= 5:
            # GeneralInfo — check for fastwalk keys
            subcmd = struct.unpack(">H", data[3:5])[0]
            if subcmd == 0x01 and len(data) >= 29:
                # Set 6 fastwalk keys
                keys = []
                for i in range(6):
                    off = 5 + i * 4
                    keys.append(struct.unpack(">I", data[off : off + 4])[0])
                walker.set_fast_walk_keys(keys)
                logger.info("fastwalk_keys_set", keys=[f"0x{k:08X}" for k in keys[:5]])
            elif subcmd == 0x02 and len(data) >= 9:
                key = struct.unpack(">I", data[5:9])[0]
                walker.add_fast_walk_key(key)
                logger.debug("fastwalk_key_added", key=f"0x{key:08X}")

        elif packet_id == 0x20:  # MobileUpdate
            r = PacketReader(data[1:])
            serial = r.read_u32()
            r.skip(3)  # graphic + graphic_inc
            r.skip(2)  # hue
            r.skip(1)  # flags
            x = r.read_u16()
            y = r.read_u16()
            r.skip(2)  # server_id
            direction = r.read_u8() & 0x07
            z = r.read_i8()
            # Update walker state (like ClassicUO: 0x20 resets walker)
            walker.player_x = x
            walker.player_y = y
            walker.player_z = z
            walker.player_direction = direction
            walker.walking_failed = False
            walker.steps_count = 0
            logger.debug("player_update", pos=f"({x},{y},{z})", dir=direction)

        elif packet_id == 0x21:  # DenyWalk
            r = PacketReader(data[1:])
            seq = r.read_u8()
            x = r.read_u16()
            y = r.read_u16()
            direction = r.read_u8() & 0x07
            z = r.read_i8()
            walker.deny_walk(seq, x, y, z)
            walker.player_direction = direction
            logger.info("walk_denied", seq=seq, pos=f"({x},{y},{z})")

        elif packet_id == 0x22:  # ConfirmWalk
            r = PacketReader(data[1:])
            seq = r.read_u8()
            walker.confirm_walk(seq)
            logger.info("walk_confirmed", seq=seq)

        elif packet_id == 0x1C:  # ASCII Talk
            if len(data) > 8:
                r = PacketReader(data[3:])  # variable: skip id + length
                serial = r.read_u32()
                r.skip(2)  # graphic
                msg_type = r.read_u8()
                r.skip(4)  # hue, font
                name = r.read_ascii(30)
                text = r.read_ascii_remaining()
                logger.info("speech", name=name, text=text, type=msg_type)

        elif packet_id == 0xAE:  # Unicode Talk
            if len(data) > 48:
                r = PacketReader(data[3:])
                serial = r.read_u32()
                r.skip(2)  # graphic
                msg_type = r.read_u8()
                r.skip(4)  # hue, font
                lang = r.read_ascii(4)
                name = r.read_ascii(30)
                text = r.read_unicode_remaining()
                logger.info("speech", name=name, text=text, lang=lang, type=msg_type)

        elif packet_id == 0x73:  # Ping
            await conn.send_packet(build_ping(data[1] if len(data) > 1 else 0))

        elif packet_id in (0x1D, 0x77, 0x78, 0x2E, 0x4E, 0x4F, 0x6D, 0xBC, 0x55):
            pass  # common packets, skip logging

        else:
            logger.debug("packet_recv", packet_id=f"0x{packet_id:02X}", size=len(data))


# ---------------------------------------------------------------------------
# Walk loop
# ---------------------------------------------------------------------------

async def wander_loop(conn: UoConnection) -> None:
    """Walk in random directions with proper turn-then-move logic."""
    from anima.client.packets import build_unicode_speech

    await asyncio.sleep(2.0)  # wait for world to load and fastwalk keys

    # Say hello
    await conn.send_packet(build_unicode_speech("Hello from Anima!"))
    logger.info("speech_sent", text="Hello from Anima!")

    direction = random.randint(0, 7)

    while conn.connected:
        if not walker.can_walk():
            await asyncio.sleep(0.05)
            continue

        # Occasionally change target direction
        if random.random() < 0.2:
            direction = random.randint(0, 7)

        # If facing different direction, turn first (100ms)
        current_dir = walker.player_direction
        if current_dir != direction:
            # Turn: send walk packet with new direction but no actual movement
            seq = walker.next_sequence()
            fastwalk = walker.pop_fast_walk_key()
            pkt = build_walk_request(direction, seq, fastwalk)
            await conn.send_packet(pkt)
            walker.steps_count += 1
            walker.last_step_time = asyncio.get_event_loop().time() * 1000 + TURN_DELAY_MS
            walker.player_direction = direction
            logger.debug("turn", dir=direction, seq=seq, fwk=f"0x{fastwalk:08X}")
        else:
            # Walk forward
            seq = walker.next_sequence()
            fastwalk = walker.pop_fast_walk_key()
            pkt = build_walk_request(direction, seq, fastwalk)
            await conn.send_packet(pkt)
            walker.steps_count += 1
            walker.last_step_time = asyncio.get_event_loop().time() * 1000 + WALK_DELAY_MS
            logger.debug("walk", dir=direction, seq=seq, fwk=f"0x{fastwalk:08X}")

        await asyncio.sleep(0.1)  # small yield


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run(host: str, port: int, username: str, password: str) -> None:
    conn = UoConnection()

    try:
        result = await conn.login(host, port, username, password)
        walker.player_x = result.x
        walker.player_y = result.y
        walker.player_z = result.z
        walker.player_direction = result.direction
        logger.info(
            "agent_ready",
            serial=f"0x{result.serial:08X}",
            position=f"({result.x}, {result.y}, {result.z})",
        )

        await asyncio.gather(
            recv_loop(conn),
            wander_loop(conn),
        )
    except ConnectionError as e:
        logger.error("connection_error", error=str(e))
    except KeyboardInterrupt:
        logger.info("shutting_down")


def main() -> None:
    parser = argparse.ArgumentParser(description="Anima — UO AI Player")
    parser.add_argument("--host", default="127.0.0.1", help="Server host")
    parser.add_argument("--port", type=int, default=2593, help="Server port")
    parser.add_argument("--user", default="admin", help="Account username")
    parser.add_argument("--pass", dest="password", default="admin", help="Account password")
    args = parser.parse_args()

    asyncio.run(run(args.host, args.port, args.user, args.password))


if __name__ == "__main__":
    main()
