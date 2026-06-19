"""HuntNearby must request a target's health bar (0x34 status request).

ServUO does not stream a foe's hits/hits_max on its own; a real client asks for
the mobile's basic-stats status bar (StatusRequest type 4) when it engages. The
combat loop relied on hits/hits_max being populated for both its focus-fire sort
key (_find_target) and its health-bar death check (hits_max > 0 and hits <= 0),
yet never sent the request — so for un-queried mobs both stayed at the 0 default
and silently went inert. ``_request_target_status`` sends it exactly once per
serial (gated through the blackboard, like ClassicUO's HitsRequest de-dup).
"""
import struct
from types import SimpleNamespace

import pytest

from anima.client.packets import build_status_request
from anima.procedures.combat_loop import _request_target_status


class _FakeConn:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_packet(self, data: bytes) -> None:
        self.sent.append(data)


def _ctx(conn: _FakeConn) -> SimpleNamespace:
    return SimpleNamespace(conn=conn, blackboard={})


def _decode_status_request(pkt: bytes) -> tuple[int, int, int]:
    """Return (request_type, serial, total_len) for a 0x34 packet."""
    assert pkt[0] == 0x34, "must be a StatusRequest (0x34)"
    pattern = struct.unpack_from(">I", pkt, 1)[0]
    assert pattern == 0xEDEDEDED, "0x34 carries the 0xEDEDEDED pattern (ClassicUO Send_StatusRequest)"
    request_type = pkt[5]
    serial = struct.unpack_from(">I", pkt, 6)[0]
    return request_type, serial, len(pkt)


@pytest.mark.asyncio
async def test_request_target_status_sends_once_per_serial():
    conn = _FakeConn()
    ctx = _ctx(conn)

    assert await _request_target_status(ctx, 0xAABBCCDD) is True
    # Same serial again (e.g. next tick / re-attack) must NOT re-send.
    assert await _request_target_status(ctx, 0xAABBCCDD) is False
    assert len(conn.sent) == 1, "exactly one status request for a single foe"

    request_type, serial, total_len = _decode_status_request(conn.sent[0])
    assert request_type == 4, "type 4 = basic stats (carries hits/hits_max)"
    assert serial == 0xAABBCCDD
    assert total_len == 10, "0x34 is a fixed 10-byte packet"


@pytest.mark.asyncio
async def test_request_target_status_sends_for_each_new_target():
    conn = _FakeConn()
    ctx = _ctx(conn)

    await _request_target_status(ctx, 0x01)
    await _request_target_status(ctx, 0x02)  # retarget -> new health bar
    await _request_target_status(ctx, 0x01)  # back to first -> already known

    assert len(conn.sent) == 2, "one request per distinct foe, no re-query"
    serials = {_decode_status_request(p)[1] for p in conn.sent}
    assert serials == {0x01, 0x02}


@pytest.mark.asyncio
async def test_request_target_status_matches_builder_bytes():
    # The packet on the wire must be byte-identical to the canonical builder
    # (pins the 0x34 layout: [0x34][0xEDEDEDED][type][serial], 10 bytes BE).
    conn = _FakeConn()
    await _request_target_status(_ctx(conn), 0x12345678)
    assert conn.sent[0] == build_status_request(4, 0x12345678)
