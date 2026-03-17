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
