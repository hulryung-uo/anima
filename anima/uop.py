"""UOP (Ultima Online Patch) file reader."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _uop_hash(s: str) -> int:
    """Compute UOP entry hash (Jenkins HashLittle2)."""
    length = len(s)
    ebx = edi = esi = (length + 0xDEADBEEF) & 0xFFFFFFFF

    i = 0
    while i + 12 < length:
        edi = (edi + (ord(s[i+7]) << 24 | ord(s[i+6]) << 16 | ord(s[i+5]) << 8 | ord(s[i+4]))) & 0xFFFFFFFF
        esi = (esi + (ord(s[i+11]) << 24 | ord(s[i+10]) << 16 | ord(s[i+9]) << 8 | ord(s[i+8]))) & 0xFFFFFFFF
        edx = ((ord(s[i+3]) << 24 | ord(s[i+2]) << 16 | ord(s[i+1]) << 8 | ord(s[i])) - esi) & 0xFFFFFFFF

        edx = (edx + ebx) & 0xFFFFFFFF
        edx ^= ((esi >> 28) | (esi << 4)) & 0xFFFFFFFF
        edx &= 0xFFFFFFFF
        esi = (esi + edi) & 0xFFFFFFFF

        edi = (edi - edx) & 0xFFFFFFFF
        edi ^= ((edx >> 26) | (edx << 6)) & 0xFFFFFFFF
        edi &= 0xFFFFFFFF
        edx = (edx + esi) & 0xFFFFFFFF

        esi = (esi - edi) & 0xFFFFFFFF
        esi ^= ((edi >> 24) | (edi << 8)) & 0xFFFFFFFF
        esi &= 0xFFFFFFFF
        edi = (edi + edx) & 0xFFFFFFFF

        ebx = (edx - esi) & 0xFFFFFFFF
        ebx ^= ((esi >> 16) | (esi << 16)) & 0xFFFFFFFF
        ebx &= 0xFFFFFFFF
        esi = (esi + edi) & 0xFFFFFFFF

        edi = (edi - ebx) & 0xFFFFFFFF
        edi ^= ((ebx >> 13) | (ebx << 19)) & 0xFFFFFFFF
        edi &= 0xFFFFFFFF
        ebx = (ebx + esi) & 0xFFFFFFFF

        esi = (esi - edi) & 0xFFFFFFFF
        esi ^= ((edi >> 28) | (edi << 4)) & 0xFFFFFFFF
        esi &= 0xFFFFFFFF
        edi = (edi + ebx) & 0xFFFFFFFF

        i += 12

    remaining = length - i
    if remaining > 0:
        # Fall-through switch for remaining bytes
        if remaining >= 12: esi = (esi + (ord(s[i+11]) << 24)) & 0xFFFFFFFF  # noqa: E701
        if remaining >= 11: esi = (esi + (ord(s[i+10]) << 16)) & 0xFFFFFFFF  # noqa: E701
        if remaining >= 10: esi = (esi + (ord(s[i+9]) << 8)) & 0xFFFFFFFF  # noqa: E701
        if remaining >= 9: esi = (esi + ord(s[i+8])) & 0xFFFFFFFF  # noqa: E701
        if remaining >= 8: edi = (edi + (ord(s[i+7]) << 24)) & 0xFFFFFFFF  # noqa: E701
        if remaining >= 7: edi = (edi + (ord(s[i+6]) << 16)) & 0xFFFFFFFF  # noqa: E701
        if remaining >= 6: edi = (edi + (ord(s[i+5]) << 8)) & 0xFFFFFFFF  # noqa: E701
        if remaining >= 5: edi = (edi + ord(s[i+4])) & 0xFFFFFFFF  # noqa: E701
        if remaining >= 4: ebx = (ebx + (ord(s[i+3]) << 24)) & 0xFFFFFFFF  # noqa: E701
        if remaining >= 3: ebx = (ebx + (ord(s[i+2]) << 16)) & 0xFFFFFFFF  # noqa: E701
        if remaining >= 2: ebx = (ebx + (ord(s[i+1]) << 8)) & 0xFFFFFFFF  # noqa: E701
        if remaining >= 1: ebx = (ebx + ord(s[i])) & 0xFFFFFFFF  # noqa: E701

        esi = ((esi ^ edi) - ((edi >> 18) | (edi << 14))) & 0xFFFFFFFF
        ecx = ((esi ^ ebx) - ((esi >> 21) | (esi << 11))) & 0xFFFFFFFF
        edi = ((edi ^ ecx) - ((ecx >> 7) | (ecx << 25))) & 0xFFFFFFFF
        esi = ((esi ^ edi) - ((edi >> 16) | (edi << 16))) & 0xFFFFFFFF
        edx = ((esi ^ ecx) - ((esi >> 28) | (esi << 4))) & 0xFFFFFFFF
        edi = ((edi ^ edx) - ((edx >> 18) | (edx << 14))) & 0xFFFFFFFF
        eax = ((esi ^ edi) - ((edi >> 8) | (edi << 24))) & 0xFFFFFFFF

        return (edi << 32) | eax

    return (esi << 32) | 0


class UopEntry:
    __slots__ = ("offset", "compressed_size", "decompressed_size",
                 "hash", "compression")

    def __init__(self, offset: int, compressed_size: int,
                 decompressed_size: int, file_hash: int,
                 compression: int) -> None:
        self.offset = offset
        self.compressed_size = compressed_size
        self.decompressed_size = decompressed_size
        self.hash = file_hash
        self.compression = compression


class UopReader:
    """Read UOP container files and extract entries by hash."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data = path.read_bytes()
        self._entries: dict[int, UopEntry] = {}
        self._parse()

    def _parse(self) -> None:
        data = self._data
        magic = struct.unpack_from("<I", data, 0)[0]
        if magic != 0x50594D:
            raise ValueError(f"Not a UOP file: bad magic 0x{magic:X}")

        next_block = struct.unpack_from("<q", data, 12)[0]
        block_capacity = struct.unpack_from("<I", data, 20)[0]

        while next_block != 0:
            count = struct.unpack_from("<i", data, next_block)[0]
            next_block = struct.unpack_from("<q", data, next_block + 4)[0]

            pos = next_block - count * 34 if next_block != 0 else len(data) - count * 34
            # Actually, entries start right after the block header (count + next_block)
            # Let me re-read: block header is at next_block_prev
            # count(4) + next(8) = 12 bytes header, then entries
            pass

        # Re-parse properly
        self._entries.clear()
        data = self._data
        next_block_offset = struct.unpack_from("<q", data, 12)[0]

        while next_block_offset != 0 and next_block_offset < len(data):
            pos = next_block_offset
            count = struct.unpack_from("<i", data, pos)[0]
            pos += 4
            next_block_offset = struct.unpack_from("<q", data, pos)[0]
            pos += 8

            for _ in range(count):
                if pos + 34 > len(data):
                    break
                offset = struct.unpack_from("<q", data, pos)[0]
                header_len = struct.unpack_from("<i", data, pos + 8)[0]
                compressed = struct.unpack_from("<i", data, pos + 12)[0]
                decompressed = struct.unpack_from("<i", data, pos + 16)[0]
                file_hash = struct.unpack_from("<Q", data, pos + 20)[0]
                compression = struct.unpack_from("<h", data, pos + 32)[0]
                pos += 34

                if offset == 0 or compressed == 0:
                    continue

                self._entries[file_hash] = UopEntry(
                    offset=offset + header_len,
                    compressed_size=compressed,
                    decompressed_size=decompressed,
                    file_hash=file_hash,
                    compression=compression,
                )

    def get_by_hash(self, file_hash: int) -> bytes | None:
        """Get decompressed data for a UOP entry by hash."""
        entry = self._entries.get(file_hash)
        if entry is None:
            return None
        raw = self._data[entry.offset : entry.offset + entry.compressed_size]
        if entry.compression == 1:
            return zlib.decompress(raw)
        return raw

    def get_by_pattern(self, pattern: str, index: int) -> bytes | None:
        """Get data by format pattern + index, e.g. 'build/map0legacymul/{0:08d}.dat'."""
        path = pattern.format(index)
        h = _uop_hash(path)
        return self.get_by_hash(h)

    @property
    def entry_count(self) -> int:
        return len(self._entries)
