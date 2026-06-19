"""Game-stream framing: server-sent variable packets ServUO emits but that were
absent from PACKET_LENGTHS.

ServUO emits these via the single-arg ``Packet(int packetID)`` constructor,
which makes them *variable-length* (a 2-byte BE length prefix follows the id):

  * 0xDA — Mahjong relay (Scripts/Items/Containers/Mahjong/Packets.cs:
    MahjongJoinGame / MahjongPlayersInfo / ... all ``base(0xDA)``).
  * 0x51 — CompactServerInfo (Scripts/Services/RemoteAdmin/Packets.cs:136,
    ``base(0x51)``).

ClassicUO's PacketsTable lists both as -1, which ClassicUO frames as variable
(it reads bytes 1-2 as the BE total length). anima, however, returns -1 for an
id that is simply *missing* from PACKET_LENGTHS, and ``_recv_game_packet`` then
takes the "unknown" branch: it discards a single byte and re-scans. The
packet's length-prefix bytes are then misread as the next packet id, cascading
into a full desync that corrupts (and ultimately stalls) the rest of the game
stream. Declaring them variable (0) lets the normal length-prefixed path
consume each one exactly, leaving the stream in sync even though the agent has
no handler for them. Mirrors the precedent set for 0x8B/0x81/0xC3.
"""

from __future__ import annotations

import asyncio

import pytest

from anima.client.connection import UoConnection
from anima.client.packets import get_packet_length


@pytest.mark.parametrize("packet_id", [0xDA, 0x51])
def test_missing_server_packets_are_framed_variable(packet_id: int):
    # 0 == variable-length (2-byte length prefix); -1 == unknown (desync).
    assert get_packet_length(packet_id) == 0


class _EmptyReader:
    """Stub StreamReader that yields no further compressed bytes."""

    async def read(self, n: int) -> bytes:
        await asyncio.sleep(0)
        return b""


@pytest.mark.asyncio
@pytest.mark.parametrize("packet_id", [0xDA, 0x51])
async def test_variable_packet_does_not_desync_game_stream(packet_id: int):
    """A variable-length frame sandwiched between two fixed 0x22 frames.

    The receiver must return the leading 0x22, consume the full variable frame
    by its declared length, and still recover the trailing 0x22 — instead of
    mistaking the length prefix for a packet id and losing the rest of the
    stream.
    """
    conn = UoConnection(timeout=0.5)
    conn._game_mode = True
    conn._reader = _EmptyReader()  # type: ignore[assignment]

    # 0x22 ConfirmWalk is fixed length 3.
    first = bytes([0x22, 0x01, 0x02])
    # Variable frame: [id][len=7 BE][4 payload bytes].
    var_frame = bytes([packet_id, 0x00, 0x07, 0xDE, 0xAD, 0xBE, 0xEF])
    second = bytes([0x22, 0x0A, 0x0B])
    conn._recv_buffer.extend(first + var_frame + second)

    received: list[tuple[int, bytes]] = []
    for _ in range(3):
        pid, data = await conn._recv_game_packet(timeout=0.5)
        received.append((pid, data))

    assert received == [
        (0x22, first),
        (packet_id, var_frame),
        (0x22, second),
    ]
    # The whole buffer was consumed cleanly — no leftover desynced bytes.
    assert len(conn._recv_buffer) == 0
