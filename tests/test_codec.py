"""Tests for packet codec and Huffman decompression."""

from anima.client.codec import PacketReader, PacketWriter, huffman_decompress


def test_packet_writer_basic():
    w = PacketWriter()
    w.write_u8(0x80)
    w.write_u16(0x1234)
    w.write_u32(0xDEADBEEF)
    data = w.to_bytes()
    assert data == b"\x80\x12\x34\xDE\xAD\xBE\xEF"


def test_packet_writer_ascii():
    w = PacketWriter()
    w.write_ascii("hello", 10)
    data = w.to_bytes()
    assert len(data) == 10
    assert data[:5] == b"hello"
    assert data[5:] == b"\x00" * 5


def test_packet_reader_basic():
    data = b"\x80\x12\x34\xDE\xAD\xBE\xEF"
    r = PacketReader(data)
    assert r.read_u8() == 0x80
    assert r.read_u16() == 0x1234
    assert r.read_u32() == 0xDEADBEEF
    assert r.remaining == 0


def test_packet_reader_ascii():
    data = b"hello\x00\x00\x00\x00\x00"
    r = PacketReader(data)
    assert r.read_ascii(10) == "hello"


def test_packet_reader_signed():
    data = b"\xFF\xFF\xFE"  # -1 as i8, -2 as i8
    r = PacketReader(data)
    assert r.read_i8() == -1
    # read_i16 of 0xFFFE = -2
    data2 = b"\xFF\xFE"
    r2 = PacketReader(data2)
    assert r2.read_i16() == -2


def test_read_unicode_remaining_stops_at_first_nul():
    """read_unicode_remaining must mirror ClassicUO ReadUnicodeBE: stop at the
    first 0x0000 UTF-16 code unit, not merely rstrip trailing NULs. ServUO's
    WriteBigUniNull terminates speech text with a 0x0000; anything after that
    terminator (an embedded NUL plus text, or frame padding) must be dropped."""
    # "Hello" + NUL terminator + trailing junk code units.
    raw = "Hello".encode("utf-16-be") + b"\x00\x00" + "XYZ".encode("utf-16-be")
    r = PacketReader(raw)
    assert r.read_unicode_remaining() == "Hello"

    # A plain NUL-terminated string with no trailing bytes still works.
    r2 = PacketReader("Bjorn".encode("utf-16-be") + b"\x00\x00")
    assert r2.read_unicode_remaining() == "Bjorn"

    # No terminator at all: consume the whole buffer.
    r3 = PacketReader("end".encode("utf-16-be"))
    assert r3.read_unicode_remaining() == "end"


def test_read_unicode_be_fixed_length_stops_at_first_nul():
    """read_unicode_be(length) must mirror ClassicUO's fixed-length
    ReadUnicodeBE (StackDataReader.ReadString -> GetIndexOfZero): a NUL-padded
    field ends at the FIRST 0x0000 code unit, but the cursor advances the full
    `length` units regardless. The old rstrip-only path leaked anything before a
    trailing run of NULs (e.g. an embedded NUL followed by more text/padding)."""
    # "Hi" + NUL terminator + "Bob" -> 6 code units (12 bytes). Real client
    # shows "Hi"; the old rstrip path wrongly yielded "Hi\x00\x00Bob".
    raw = "Hi".encode("utf-16-be") + b"\x00\x00" + "Bob".encode("utf-16-be")
    r = PacketReader(raw)
    assert r.read_unicode_be(6) == "Hi"
    # Cursor advanced the full length*2 bytes even though the value ended early.
    assert r.remaining == 0

    # Plain NUL-padded field (the common case): trailing NULs are dropped.
    padded = "Name".encode("utf-16-be") + b"\x00\x00\x00\x00"
    r2 = PacketReader(padded)
    assert r2.read_unicode_be(6) == "Name"
    assert r2.remaining == 0

    # A field that fills its full width with no NUL keeps every code unit and
    # consumes exactly length*2 bytes (no over/under-read).
    full = "World".encode("utf-16-be")
    r3 = PacketReader(full + b"\xAB\xCD")  # extra trailing byte must remain
    assert r3.read_unicode_be(5) == "World"
    assert r3.remaining == 2


def test_huffman_roundtrip():
    """Verify Huffman decompression works with known data.

    We build expected compressed bytes by encoding a known string
    using the Huffman table, then verify decompression recovers it.
    """
    # Simple test: compress [0x00] manually
    # HUFFMAN_TABLE[0] = (2, 0x000) → 2 bits: 00
    # HUFFMAN_TABLE[256] = (4, 0x00D) → terminal: 1101
    # Total bits: 00 1101 → 001101 00 (padded) = 0x34
    compressed = bytes([0x34])
    result = huffman_decompress(compressed, 1)
    assert result == bytes([0x00])


def test_huffman_multi_byte():
    """Test decompression of multiple bytes."""
    # Byte 0x00 → (2 bits, 0x000) → 00
    # Byte 0x00 → (2 bits, 0x000) → 00
    # Terminal → (4 bits, 0x00D) → 1101
    # Total: 00 00 1101 → 0000 1101 = 0x0D
    compressed = bytes([0x0D])
    result = huffman_decompress(compressed, 2)
    assert result == bytes([0x00, 0x00])


def test_packet_build_seed():
    from anima.client.packets import build_seed

    data = build_seed(0x01020304, 7, 0, 102, 3)
    assert len(data) == 21
    assert data[0] == 0xEF
    r = PacketReader(data[1:])
    assert r.read_u32() == 0x01020304
    assert r.read_u32() == 7
    assert r.read_u32() == 0
    assert r.read_u32() == 102
    assert r.read_u32() == 3


def test_packet_build_game_seed_is_raw_4_bytes():
    """Phase-2 game-server seed is a bare 4-byte BE key, not a 0xEF packet.

    Matches ClassicUO LoginScene.HandleRelayServerPacket, which sends the raw
    seed bytes before Send_SecondLogin on the game connection.
    """
    from anima.client.packets import build_game_seed, build_seed

    auth_key = 0x9C69B58C
    data = build_game_seed(auth_key)

    # Exactly 4 bytes, Big-Endian, and crucially NOT the 21-byte 0xEF packet.
    assert len(data) == 4
    assert data == bytes([0x9C, 0x69, 0xB5, 0x8C])
    assert data[0] != 0xEF
    assert PacketReader(data).read_u32() == auth_key
    assert data != build_seed(seed=auth_key)
    assert len(build_seed(seed=auth_key)) == 21


def test_packet_build_account_login():
    from anima.client.packets import build_account_login

    data = build_account_login("admin", "admin")
    assert len(data) == 62
    assert data[0] == 0x80
    r = PacketReader(data[1:])
    assert r.read_ascii(30) == "admin"
    assert r.read_ascii(30) == "admin"


def test_packet_build_walk():
    from anima.client.packets import build_walk_request

    data = build_walk_request(direction=0x82, seq=5, fastwalk=0)
    assert len(data) == 7
    assert data[0] == 0x02
    assert data[1] == 0x82  # direction with run flag
    assert data[2] == 5     # sequence


# --- Item interaction builders (0x06 / 0x07 / 0x08 / 0x13) ---------------
# Byte layouts verified against ClassicUO OutgoingPackets.cs
# (Send_DoubleClick / Send_PickUpRequest / Send_DropRequest /
# Send_EquipRequest) and ServUO PacketHandlers.cs (DropReq6017 = 15 bytes,
# EquipReq = 10 bytes). These lock the wire format against silent
# regressions in fresh, otherwise-untested territory.


def test_build_double_click_layout():
    from anima.client.packets import build_double_click

    data = build_double_click(0x40001234)
    assert len(data) == 5
    assert data[0] == 0x06
    assert data[1:5] == bytes([0x40, 0x00, 0x12, 0x34])  # serial, big-endian


def test_build_pick_up_layout():
    from anima.client.packets import build_pick_up

    data = build_pick_up(0x40005678, amount=5)
    assert len(data) == 7
    assert data[0] == 0x07
    assert data[1:5] == bytes([0x40, 0x00, 0x56, 0x78])  # serial BE
    assert data[5:7] == bytes([0x00, 0x05])              # amount u16 BE


def test_build_pick_up_clamps_overflowing_amount():
    # A computed amount above the u16 ceiling must clamp to 0xFFFF
    # (lift as much as possible), not wrap mod 65536 into a wrong count.
    from anima.client.packets import build_pick_up

    data = build_pick_up(0x1, amount=70000)
    assert data[5:7] == bytes([0xFF, 0xFF])  # clamped, NOT 70000 & 0xFFFF
    # Negative amounts clamp to 0 rather than wrapping to a huge stack.
    data = build_pick_up(0x1, amount=-1)
    assert data[5:7] == bytes([0x00, 0x00])


def test_build_drop_item_to_container_layout():
    # Modern 6017 drop: serial, x, y, z, grid-slot byte, container.
    # Dropping into a container uses x=y=0xFFFF (auto-place sentinel),
    # matching ClassicUO's grab-bag drop (GameActions.cs).
    from anima.client.packets import build_drop_item

    data = build_drop_item(0x4000AAAA, container=0x40001111)
    assert len(data) == 15
    assert data[0] == 0x08
    assert data[1:5] == bytes([0x40, 0x00, 0xAA, 0xAA])   # item serial BE
    assert data[5:7] == bytes([0xFF, 0xFF])               # x = 0xFFFF
    assert data[7:9] == bytes([0xFF, 0xFF])               # y = 0xFFFF
    assert data[9] == 0x00                                # z (i8)
    assert data[10] == 0x00                               # grid slot
    assert data[11:15] == bytes([0x40, 0x00, 0x11, 0x11]) # container BE


def test_build_drop_item_to_ground_layout():
    # Ground drop: explicit coords, container left at the 0xFFFFFFFF
    # "no container" sentinel.
    from anima.client.packets import build_drop_item

    data = build_drop_item(0x40002222, x=1000, y=2000, z=-5)
    assert len(data) == 15
    assert data[0] == 0x08
    assert data[5:7] == (1000).to_bytes(2, "big")
    assert data[7:9] == (2000).to_bytes(2, "big")
    assert data[9] == 0xFB                                 # z = -5 as i8
    assert data[10] == 0x00                                # grid slot
    assert data[11:15] == bytes([0xFF, 0xFF, 0xFF, 0xFF])  # no container


def test_build_drop_item_clamps_out_of_range_z():
    # z is an sbyte on the wire; a world/spawn-spot altitude outside
    # [-128, 127] must clamp (drop at the nearest valid height) rather
    # than raise struct.error and abort the pick-up -> drop sequence.
    from anima.client.packets import build_drop_item

    # Above the sbyte ceiling -> clamp to +127 (0x7F), not a struct.error
    # and NOT a two's-complement wrap (200 -> -56 -> 0xC8).
    data = build_drop_item(0x40002222, x=1000, y=2000, z=200)
    assert len(data) == 15
    assert data[9] == 0x7F

    # Below the sbyte floor -> clamp to -128 (0x80).
    data = build_drop_item(0x40002222, x=1000, y=2000, z=-200)
    assert len(data) == 15
    assert data[9] == 0x80

    # An in-range negative z is still written verbatim as two's complement.
    data = build_drop_item(0x40002222, x=1000, y=2000, z=-5)
    assert data[9] == 0xFB


def test_build_equip_item_layout():
    # 0x13: item serial, layer byte, wearer serial — 10 bytes total.
    # ServUO EquipReq seeks past serial(4)+layer(1) then reads the wearer.
    from anima.client.packets import build_equip_item

    data = build_equip_item(0x40003333, layer=0x02, mobile_serial=0x00012345)
    assert len(data) == 10
    assert data[0] == 0x13
    assert data[1:5] == bytes([0x40, 0x00, 0x33, 0x33])  # item serial BE
    assert data[5] == 0x02                               # layer (TwoHanded)
    assert data[6:10] == bytes([0x00, 0x01, 0x23, 0x45]) # wearer serial BE


# --- Huffman streaming contract (game-phase server->client) --------------
# These lock the contract that ConnectionManager._recv_game_packet relies on:
# huffman_decompress_one must round-trip real multi-symbol frames, report the
# exact bytes consumed so concatenated/spanning frames stay aligned, and return
# (None, 0) for ANY partial frame so the receiver waits for more TCP data
# instead of desyncing the stream. The pre-existing tests only exercised the
# all-zero single-frame happy path via huffman_decompress.

from anima.client.codec import _HUFFMAN_TABLE, huffman_decompress_one


def _encode_frame(payload: bytes) -> bytes:
    """Independently encode one Huffman frame (payload + terminal, zero-padded).

    Mirrors the UO server's per-packet compression so the decoder can be
    exercised against real bit streams rather than hand-computed constants.
    """
    bits: list[int] = []
    for sym in list(payload) + [256]:  # 256 = terminal symbol
        nbits, code = _HUFFMAN_TABLE[sym]
        for i in range(nbits - 1, -1, -1):
            bits.append((code >> i) & 1)
    while len(bits) % 8:
        bits.append(0)
    out = bytearray()
    for i in range(0, len(bits), 8):
        v = 0
        for b in bits[i : i + 8]:
            v = (v << 1) | b
        out.append(v)
    return bytes(out)


def test_huffman_one_roundtrip_multi_symbol():
    payload = b"Hello, Britannia! \x00\x01\xfe\xff"
    comp = _encode_frame(payload)
    res, consumed = huffman_decompress_one(comp)
    assert res == payload
    assert consumed == len(comp)


def test_huffman_one_partial_frame_returns_none():
    # Every truncation strictly before the terminal must read as "need more
    # data" (None, 0) — never a falsely-complete frame.
    comp = _encode_frame(b"a-reasonably-long-server-packet-body")
    for cut in range(1, len(comp)):
        res, consumed = huffman_decompress_one(comp[:cut])
        assert res is None and consumed == 0, (cut, res, consumed)
    # ...and the full buffer decodes cleanly.
    res, consumed = huffman_decompress_one(comp)
    assert res == b"a-reasonably-long-server-packet-body"
    assert consumed == len(comp)


def test_huffman_one_spanning_frames_consumed():
    # Two independently-compressed frames back-to-back: decode_one must stop at
    # the first terminal and report exactly frame-1's byte length so the caller
    # can advance and decode frame-2 from the remainder.
    f1, f2 = b"frame-one", b"2"
    c1, c2 = _encode_frame(f1), _encode_frame(f2)
    res, consumed = huffman_decompress_one(c1 + c2)
    assert res == f1 and consumed == len(c1)
    res2, consumed2 = huffman_decompress_one((c1 + c2)[consumed:])
    assert res2 == f2 and consumed2 == len(c2)


def test_huffman_one_random_streaming_no_desync():
    import random

    rng = random.Random(20240619)
    for _ in range(500):
        f1 = bytes(rng.randrange(256) for _ in range(rng.randint(1, 8)))
        f2 = bytes(rng.randrange(256) for _ in range(rng.randint(1, 8)))
        c1, c2 = _encode_frame(f1), _encode_frame(f2)
        buf = c1 + c2
        # Feed every byte-length prefix: the first non-None result must be the
        # complete frame-1 with the exact consumed count, and a complete frame
        # must never be missed once all its bytes are present.
        for plen in range(1, len(buf) + 1):
            res, consumed = huffman_decompress_one(buf[:plen])
            if res is not None:
                assert res == f1 and consumed == len(c1)
                break
            assert plen < len(c1), "complete frame went undetected"


def test_huffman_multi_decompresses_all_frames():
    payloads = [b"AB", b"", b"\x00\xff\x10", b"x"]
    buf = b"".join(_encode_frame(p) for p in payloads)
    assert huffman_decompress(buf, 10_000) == b"".join(payloads)
