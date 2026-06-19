"""Game-stream framing: variable-length server packets must not desync.

ServUO emits several server->client packets via the single-arg ``Packet(int
packetID)`` constructor, which makes them *variable-length* (a 2-byte length
prefix follows the id). The most reachable in-world is ``0x8B``
(DisplaySignGump, sent when a sign is examined); ``0x81`` (ChangeCharacter) and
``0xC3`` (GQRequest) are the others. ClassicUO's PacketsTable lists all three
as -1 (variable).

If such an id is missing from ``PACKET_LENGTHS``, ``get_packet_length`` returns
-1 and ``_recv_game_packet`` takes the "unknown" branch: it discards a single
byte and re-scans. The packet's length-prefix bytes are then misread as the
next packet id, cascading into a full desync that corrupts (and ultimately
stalls) the rest of the game stream. Framing them as variable (0) lets the
normal length-prefixed path consume each one exactly, leaving the stream in
sync even though the agent has no handler for them.
"""

from __future__ import annotations

import asyncio

import pytest

from anima.client.connection import UoConnection
from anima.client.packets import get_packet_length


@pytest.mark.parametrize("packet_id", [0x8B, 0x81, 0xC3])
def test_variable_server_packets_are_framed_variable(packet_id: int):
    # 0 == variable-length (2-byte length prefix); -1 == unknown (desync).
    assert get_packet_length(packet_id) == 0


class _EmptyReader:
    """Stub StreamReader that yields no further compressed bytes."""

    async def read(self, n: int) -> bytes:
        await asyncio.sleep(0)
        return b""


@pytest.mark.asyncio
async def test_variable_sign_gump_does_not_desync_game_stream():
    """A variable-length 0x8B sandwiched between two fixed 0x22 frames.

    The receiver must return the leading 0x22, consume the full 0x8B by its
    declared length, and still recover the trailing 0x22 — instead of mistaking
    the 0x8B length prefix for a packet id and losing the rest of the stream.
    """
    conn = UoConnection(timeout=0.5)
    conn._game_mode = True
    conn._reader = _EmptyReader()  # type: ignore[assignment]

    # 0x22 ConfirmWalk is fixed length 3.
    first = bytes([0x22, 0x01, 0x02])
    # 0x8B DisplaySignGump: [id][len=7 BE][4 payload bytes].
    sign_gump = bytes([0x8B, 0x00, 0x07, 0xDE, 0xAD, 0xBE, 0xEF])
    second = bytes([0x22, 0x0A, 0x0B])
    conn._recv_buffer.extend(first + sign_gump + second)

    received: list[tuple[int, bytes]] = []
    for _ in range(3):
        pid, data = await conn._recv_game_packet(timeout=0.5)
        received.append((pid, data))

    assert received == [
        (0x22, first),
        (0x8B, sign_gump),
        (0x22, second),
    ]
    # The whole buffer was consumed cleanly — no leftover desynced bytes.
    assert len(conn._recv_buffer) == 0


@pytest.mark.asyncio
async def test_unknown_id_still_resyncs_by_one_byte():
    """A genuinely-unknown id must still take the 1-byte resync path.

    Guards against over-broadening: a fixed packet following a discarded
    unknown byte must still be recoverable. 0x7F is not in PACKET_LENGTHS.
    """
    assert get_packet_length(0x7F) == -1
    conn = UoConnection(timeout=0.5)
    conn._game_mode = True
    conn._reader = _EmptyReader()  # type: ignore[assignment]

    # One stray unknown byte, then a clean fixed 0x22 frame.
    conn._recv_buffer.extend(bytes([0x7F]) + bytes([0x22, 0x05, 0x06]))

    pid, data = await conn._recv_game_packet(timeout=0.5)
    assert pid == 0x22
    assert data == bytes([0x22, 0x05, 0x06])
