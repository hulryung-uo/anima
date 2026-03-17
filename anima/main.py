"""Anima entry point — connect to servuo-rs and walk around."""

from __future__ import annotations

import argparse
import asyncio
import random

import structlog

from anima.client.connection import UoConnection
from anima.client.packets import build_ping, build_walk_request, get_packet_length

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
)
logger = structlog.get_logger()


async def recv_loop(conn: UoConnection) -> None:
    """Receive and log all game packets."""
    while conn.connected:
        try:
            packet_id, data = await conn.recv_packet(timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except (ConnectionError, EOFError):
            logger.error("connection_lost")
            break

        # Log interesting packets
        if packet_id == 0x1C:  # ASCII Talk
            if len(data) > 8:
                from anima.client.codec import PacketReader

                r = PacketReader(data[1:] if get_packet_length(0x1C) > 0 else data[3:])
                serial = r.read_u32()
                r.skip(2)  # graphic
                msg_type = r.read_u8()
                r.skip(4)  # hue, font
                name = r.read_ascii(30)
                text = r.read_ascii_remaining()
                logger.info("speech", name=name, text=text, type=msg_type)
        elif packet_id == 0xAE:  # Unicode Talk
            if len(data) > 48:
                from anima.client.codec import PacketReader

                r = PacketReader(data[3:])  # skip id + length
                serial = r.read_u32()
                r.skip(2)  # graphic
                msg_type = r.read_u8()
                r.skip(4)  # hue, font
                lang = r.read_ascii(4)
                name = r.read_ascii(30)
                text = r.read_unicode_remaining()
                logger.info("speech", name=name, text=text, lang=lang, type=msg_type)
        elif packet_id == 0x20:  # MobileUpdate
            from anima.client.codec import PacketReader

            r = PacketReader(data[1:])
            serial = r.read_u32()
            r.skip(3)  # graphic, graphic_inc
            r.skip(2)  # hue
            r.skip(1)  # flags
            x = r.read_u16()
            y = r.read_u16()
            r.skip(2)  # server_id
            direction = r.read_u8()
            z = r.read_i8()
            logger.debug(
                "mobile_update",
                serial=f"0x{serial:08X}",
                pos=f"({x},{y},{z})",
            )
        elif packet_id == 0x21:  # DenyWalk
            logger.debug("walk_denied")
        elif packet_id == 0x22:  # ConfirmWalk
            pass  # very frequent, skip logging
        elif packet_id == 0x73:  # Ping
            # Respond to ping
            await conn.send_packet(build_ping(data[1] if len(data) > 1 else 0))
        elif packet_id == 0x1D:  # DeleteObject
            pass
        else:
            logger.debug(
                "packet_recv",
                packet_id=f"0x{packet_id:02X}",
                size=len(data),
            )


async def wander_loop(conn: UoConnection) -> None:
    """Walk in random directions."""
    seq = 1
    direction = 0

    await asyncio.sleep(1.0)  # wait for world to load

    while conn.connected:
        # Occasionally change direction
        if random.random() < 0.3:
            direction = random.randint(0, 7)

        packet = build_walk_request(direction, seq)
        await conn.send_packet(packet)

        seq = seq + 1 if seq < 255 else 1
        await asyncio.sleep(0.4)  # 400ms walk delay


async def run(host: str, port: int, username: str, password: str) -> None:
    conn = UoConnection()

    try:
        result = await conn.login(host, port, username, password)
        logger.info(
            "agent_ready",
            serial=f"0x{result.serial:08X}",
            position=f"({result.x}, {result.y}, {result.z})",
        )

        # Run recv and wander concurrently
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
