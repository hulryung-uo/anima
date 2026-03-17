"""Packet binary encoding/decoding and Huffman decompression."""

from __future__ import annotations

import struct
from io import BytesIO

# fmt: off
# Huffman table: 257 entries, each [bit_count, code_value]
# Index 0-255 = byte values, index 256 = terminal symbol
_HUFFMAN_TABLE: list[tuple[int, int]] = [
    (0x2, 0x000), (0x5, 0x01F), (0x6, 0x022), (0x7, 0x034), (0x7, 0x075), (0x6, 0x028), (0x6, 0x03B), (0x7, 0x032),
    (0x8, 0x0E0), (0x8, 0x062), (0x7, 0x056), (0x8, 0x079), (0x9, 0x19D), (0x8, 0x097), (0x6, 0x02A), (0x7, 0x057),
    (0x8, 0x071), (0x8, 0x05B), (0x9, 0x1CC), (0x8, 0x0A7), (0x7, 0x025), (0x7, 0x04F), (0x8, 0x066), (0x8, 0x07D),
    (0x9, 0x191), (0x9, 0x1CE), (0x7, 0x03F), (0x9, 0x090), (0x8, 0x059), (0x8, 0x07B), (0x8, 0x091), (0x8, 0x0C6),
    (0x6, 0x02D), (0x9, 0x186), (0x8, 0x06F), (0x9, 0x093), (0xA, 0x1CC), (0x8, 0x05A), (0xA, 0x1AE), (0xA, 0x1C0),
    (0x9, 0x148), (0x9, 0x14A), (0x9, 0x082), (0xA, 0x19F), (0x9, 0x171), (0x9, 0x120), (0x9, 0x0E7), (0xA, 0x1F3),
    (0x9, 0x14B), (0x9, 0x100), (0x9, 0x190), (0x6, 0x013), (0x9, 0x161), (0x9, 0x125), (0x9, 0x133), (0x9, 0x195),
    (0x9, 0x173), (0x9, 0x1CA), (0x9, 0x086), (0x9, 0x1E9), (0x9, 0x0DB), (0x9, 0x1EC), (0x9, 0x08B), (0x9, 0x085),
    (0x5, 0x00A), (0x8, 0x096), (0x8, 0x09C), (0x9, 0x1C3), (0x9, 0x19C), (0x9, 0x08F), (0x9, 0x18F), (0x9, 0x091),
    (0x9, 0x087), (0x9, 0x0C6), (0x9, 0x177), (0x9, 0x089), (0x9, 0x0D6), (0x9, 0x08C), (0x9, 0x1EE), (0x9, 0x1EB),
    (0x9, 0x084), (0x9, 0x164), (0x9, 0x175), (0x9, 0x1CD), (0x8, 0x05E), (0x9, 0x088), (0x9, 0x12B), (0x9, 0x172),
    (0x9, 0x10A), (0x9, 0x08D), (0x9, 0x13A), (0x9, 0x11C), (0xA, 0x1E1), (0xA, 0x1E0), (0x9, 0x187), (0xA, 0x1DC),
    (0xA, 0x1DF), (0x7, 0x074), (0x9, 0x19F), (0x8, 0x08D), (0x8, 0x0E4), (0x7, 0x079), (0x9, 0x0EA), (0x9, 0x0E1),
    (0x8, 0x040), (0x7, 0x041), (0x9, 0x10B), (0x9, 0x0B0), (0x8, 0x06A), (0x8, 0x0C1), (0x7, 0x071), (0x7, 0x078),
    (0x8, 0x0B1), (0x9, 0x14C), (0x7, 0x043), (0x8, 0x076), (0x7, 0x066), (0x7, 0x04D), (0x9, 0x08A), (0x6, 0x02F),
    (0x8, 0x0C9), (0x9, 0x0CE), (0x9, 0x149), (0x9, 0x160), (0xA, 0x1BA), (0xA, 0x19E), (0xA, 0x39F), (0x9, 0x0E5),
    (0x9, 0x194), (0x9, 0x184), (0x9, 0x126), (0x7, 0x030), (0x8, 0x06C), (0x9, 0x121), (0x9, 0x1E8), (0xA, 0x1C1),
    (0xA, 0x11D), (0xA, 0x163), (0xA, 0x385), (0xA, 0x3DB), (0xA, 0x17D), (0xA, 0x106), (0xA, 0x397), (0xA, 0x24E),
    (0x7, 0x02E), (0x8, 0x098), (0xA, 0x33C), (0xA, 0x32E), (0xA, 0x1E9), (0x9, 0x0BF), (0xA, 0x3DF), (0xA, 0x1DD),
    (0xA, 0x32D), (0xA, 0x2ED), (0xA, 0x30B), (0xA, 0x107), (0xA, 0x2E8), (0xA, 0x3DE), (0xA, 0x125), (0xA, 0x1E8),
    (0x9, 0x0E9), (0xA, 0x1CD), (0xA, 0x1B5), (0x9, 0x165), (0xA, 0x232), (0xA, 0x2E1), (0xB, 0x3AE), (0xB, 0x3C6),
    (0xB, 0x3E2), (0xA, 0x205), (0xA, 0x29A), (0xA, 0x248), (0xA, 0x2CD), (0xA, 0x23B), (0xB, 0x3C5), (0xA, 0x251),
    (0xA, 0x2E9), (0xA, 0x252), (0x9, 0x1EA), (0xB, 0x3A0), (0xB, 0x391), (0xA, 0x23C), (0xB, 0x392), (0xB, 0x3D5),
    (0xA, 0x233), (0xA, 0x2CC), (0xB, 0x390), (0xA, 0x1BB), (0xB, 0x3A1), (0xB, 0x3C4), (0xA, 0x211), (0xA, 0x203),
    (0x9, 0x12A), (0xA, 0x231), (0xB, 0x3E0), (0xA, 0x29B), (0xB, 0x3D7), (0xA, 0x202), (0xB, 0x3AD), (0xA, 0x213),
    (0xA, 0x253), (0xA, 0x32C), (0xA, 0x23D), (0xA, 0x23F), (0xA, 0x32F), (0xA, 0x11C), (0xA, 0x384), (0xA, 0x31C),
    (0xA, 0x17C), (0xA, 0x30A), (0xA, 0x2E0), (0xA, 0x276), (0xA, 0x250), (0xB, 0x3E3), (0xA, 0x396), (0xA, 0x18F),
    (0xA, 0x204), (0xA, 0x206), (0xA, 0x230), (0xA, 0x265), (0xA, 0x212), (0xA, 0x23E), (0xB, 0x3AC), (0xB, 0x393),
    (0xB, 0x3E1), (0xA, 0x1DE), (0xB, 0x3D6), (0xA, 0x31D), (0xB, 0x3E5), (0xB, 0x3E4), (0xA, 0x207), (0xB, 0x3C7),
    (0xA, 0x277), (0xB, 0x3D4), (0x8, 0x0C0), (0xA, 0x162), (0xA, 0x3DA), (0xA, 0x124), (0xA, 0x1B4), (0xA, 0x264),
    (0xA, 0x33D), (0xA, 0x1D1), (0xA, 0x1AF), (0xA, 0x39E), (0xA, 0x24F), (0xB, 0x373), (0xA, 0x249), (0xB, 0x372),
    (0x9, 0x167), (0xA, 0x210), (0xA, 0x23A), (0xA, 0x1B8), (0xB, 0x3AF), (0xA, 0x18E), (0xA, 0x2EC), (0x7, 0x062),
    # Terminal symbol (index 256)
    (0x4, 0x00D),
]
# fmt: on

_MAX_BITS = 11
_DECODE_TABLE: list[tuple[int, int]] | None = None


def _build_decode_table() -> list[tuple[int, int]]:
    """Build a 2048-entry lookup table for fast Huffman decoding."""
    table_size = 1 << _MAX_BITS  # 2048
    decode: list[tuple[int, int]] = [(0xFFFF, 0)] * table_size

    for symbol, (num_bits, code) in enumerate(_HUFFMAN_TABLE):
        if num_bits == 0 or num_bits > _MAX_BITS:
            continue
        shift = _MAX_BITS - num_bits
        base_index = code << shift
        count = 1 << shift
        for i in range(count):
            idx = base_index | i
            if idx < table_size:
                decode[idx] = (symbol, num_bits)

    return decode


def _get_decode_table() -> list[tuple[int, int]]:
    global _DECODE_TABLE
    if _DECODE_TABLE is None:
        _DECODE_TABLE = _build_decode_table()
    return _DECODE_TABLE


def _extract_bits(data: bytes, bit_offset: int, num_bits: int) -> int:
    """Extract `num_bits` bits from data starting at `bit_offset` (MSB first)."""
    result = 0
    for i in range(num_bits):
        byte_idx = (bit_offset + i) // 8
        bit_idx = 7 - ((bit_offset + i) % 8)
        if byte_idx < len(data):
            result = (result << 1) | ((data[byte_idx] >> bit_idx) & 1)
        else:
            result <<= 1
    return result


def huffman_decompress_one(data: bytes, offset: int = 0) -> tuple[bytes, int]:
    """Decompress one Huffman-encoded packet from the data stream.

    Each server packet is independently compressed with its own terminal symbol.
    Returns (decompressed_bytes, bytes_consumed) so the caller can continue
    decompressing subsequent packets from the remaining data.
    """
    decode_table = _get_decode_table()
    output = bytearray()

    bit_pos = offset * 8
    total_bits = len(data) * 8

    while bit_pos < total_bits:
        bits_available = total_bits - bit_pos
        if bits_available < 2:
            break

        read_bits = min(bits_available, _MAX_BITS)
        window = _extract_bits(data, bit_pos, read_bits)
        if read_bits < _MAX_BITS:
            window <<= (_MAX_BITS - read_bits)

        symbol, code_len = decode_table[window]
        if code_len == 0 or symbol == 0xFFFF:
            raise ValueError(f"No matching Huffman code at bit position {bit_pos}")

        bit_pos += code_len

        if symbol == 256:  # terminal
            break

        output.append(symbol)

    # Round up to next byte boundary
    bytes_consumed = (bit_pos + 7) // 8 - offset
    return bytes(output), bytes_consumed


def huffman_decompress(data: bytes, output_len: int) -> bytes:
    """Decompress all Huffman-encoded packets from data into a single byte stream.

    Each packet is independently compressed with its own terminal symbol.
    This function decompresses all of them sequentially.
    """
    output = bytearray()
    offset = 0

    while offset < len(data) and len(output) < output_len:
        try:
            chunk, consumed = huffman_decompress_one(data, offset)
        except ValueError:
            break
        if not chunk and consumed == 0:
            break
        output.extend(chunk)
        offset += consumed

    return bytes(output)


class PacketWriter:
    """Build outgoing packets in Big-Endian format."""

    def __init__(self) -> None:
        self._buf = BytesIO()

    def write_u8(self, v: int) -> None:
        self._buf.write(struct.pack("B", v & 0xFF))

    def write_i8(self, v: int) -> None:
        self._buf.write(struct.pack("b", v))

    def write_u16(self, v: int) -> None:
        self._buf.write(struct.pack(">H", v & 0xFFFF))

    def write_u32(self, v: int) -> None:
        self._buf.write(struct.pack(">I", v & 0xFFFFFFFF))

    def write_ascii(self, s: str, length: int) -> None:
        """Write a fixed-length null-padded ASCII string."""
        encoded = s.encode("ascii", errors="replace")[:length]
        self._buf.write(encoded)
        padding = length - len(encoded)
        if padding > 0:
            self._buf.write(b"\x00" * padding)

    def write_zeros(self, count: int) -> None:
        self._buf.write(b"\x00" * count)

    def write_bytes(self, data: bytes) -> None:
        self._buf.write(data)

    def to_bytes(self) -> bytes:
        return self._buf.getvalue()

    def __len__(self) -> int:
        return self._buf.tell()


class PacketReader:
    """Parse incoming packets in Big-Endian format."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read_u8(self) -> int:
        v = self._data[self._pos]
        self._pos += 1
        return v

    def read_i8(self) -> int:
        (v,) = struct.unpack_from("b", self._data, self._pos)
        self._pos += 1
        return v

    def read_u16(self) -> int:
        (v,) = struct.unpack_from(">H", self._data, self._pos)
        self._pos += 2
        return v

    def read_i16(self) -> int:
        (v,) = struct.unpack_from(">h", self._data, self._pos)
        self._pos += 2
        return v

    def read_u32(self) -> int:
        (v,) = struct.unpack_from(">I", self._data, self._pos)
        self._pos += 4
        return v

    def read_ascii(self, length: int) -> str:
        raw = self._data[self._pos : self._pos + length]
        self._pos += length
        return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")

    def read_unicode_be(self, length: int = -1) -> str:
        """Read Big-Endian UTF-16 string. If length=-1, read until double null."""
        if length >= 0:
            raw = self._data[self._pos : self._pos + length * 2]
            self._pos += length * 2
            return raw.decode("utf-16-be", errors="replace").rstrip("\x00")
        # Read until double null
        chars = []
        while self._pos + 1 < len(self._data):
            (ch,) = struct.unpack_from(">H", self._data, self._pos)
            self._pos += 2
            if ch == 0:
                break
            chars.append(chr(ch))
        return "".join(chars)

    def read_remaining(self) -> bytes:
        data = self._data[self._pos :]
        self._pos = len(self._data)
        return data

    def read_ascii_remaining(self) -> str:
        return self.read_remaining().split(b"\x00", 1)[0].decode("ascii", errors="replace")

    def read_unicode_remaining(self) -> str:
        raw = self.read_remaining()
        return raw.decode("utf-16-be", errors="replace").rstrip("\x00")

    def skip(self, n: int) -> None:
        self._pos += n

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos

    @property
    def position(self) -> int:
        return self._pos
