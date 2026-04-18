"""Packet framing helpers for the proxy.

Wraps the same length table Anima uses (anima.client.packets.PACKET_LENGTHS)
and exposes `iter_packets()` which consumes bytes incrementally from a
buffer and yields complete packets.
"""

from __future__ import annotations

import struct
from typing import Iterator

from anima.client.packets import get_packet_length

__all__ = ["frame_one", "iter_packets", "FrameError"]


class FrameError(Exception):
    """Raised when the byte stream contains a packet with unknown length."""


def frame_one(buf: bytes) -> tuple[bytes, int] | None:
    """Try to consume one packet from the head of `buf`.

    Returns (packet_bytes, bytes_consumed) on success, or None when the
    buffer doesn't yet contain a full packet. Raises FrameError when the
    packet id has an unknown length (caller must decide how to resync).
    """
    if len(buf) < 1:
        return None

    pid = buf[0]
    length = get_packet_length(pid)

    if length > 0:
        if len(buf) < length:
            return None
        return bytes(buf[:length]), length

    if length == 0:
        if len(buf) < 3:
            return None
        total = struct.unpack(">H", buf[1:3])[0]
        if total < 3:
            raise FrameError(
                f"Variable packet 0x{pid:02X} declared total={total} < 3"
            )
        if len(buf) < total:
            return None
        return bytes(buf[:total]), total

    raise FrameError(f"Unknown packet id 0x{pid:02X}")


def iter_packets(buf: bytearray) -> Iterator[bytes]:
    """Yield complete packets from `buf`, consuming them in-place.

    Stops when no more complete packets are available. Remaining bytes stay
    in the buffer for the next call.
    """
    while buf:
        try:
            result = frame_one(bytes(buf))
        except FrameError:
            # Unknown packet — can't advance safely. Propagate so caller can log.
            raise
        if result is None:
            return
        packet, consumed = result
        del buf[:consumed]
        yield packet
