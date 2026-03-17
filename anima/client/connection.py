"""UO network connection with two-phase login flow."""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

import structlog

from anima.client.codec import PacketReader, huffman_decompress
from anima.client.packets import (
    build_account_login,
    build_game_login,
    build_play_character,
    build_seed,
    build_server_select,
    get_packet_length,
)

logger = structlog.get_logger()

DEFAULT_TIMEOUT = 10.0


@dataclass
class LoginResult:
    serial: int
    x: int
    y: int
    z: int
    direction: int
    body: int


class UoConnection:
    """Manages a single TCP connection to a UO server.

    Handles the two-phase login flow and packet framing
    (including Huffman decompression in game mode).
    """

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._game_mode = False
        self._recv_buffer = bytearray()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def _connect(self, host: str, port: int) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=DEFAULT_TIMEOUT,
        )
        self._recv_buffer.clear()
        logger.info("tcp_connected", host=host, port=port)

    async def _close(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    async def _send_raw(self, data: bytes) -> None:
        assert self._writer is not None
        self._writer.write(data)
        await self._writer.drain()

    async def send_packet(self, data: bytes) -> None:
        """Send a pre-built packet (no compression on client→server)."""
        await self._send_raw(data)
        logger.debug("packet_sent", packet_id=f"0x{data[0]:02X}", size=len(data))

    async def _read_bytes(self, n: int, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        """Read exactly n bytes from the socket."""
        assert self._reader is not None
        data = await asyncio.wait_for(self._reader.readexactly(n), timeout=timeout)
        return data

    async def _recv_raw_packet(self, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, bytes]:
        """Receive one raw (uncompressed) packet. Returns (packet_id, full_packet_bytes)."""
        assert self._reader is not None

        # Read packet ID (1 byte)
        id_byte = await self._read_bytes(1, timeout)
        packet_id = id_byte[0]
        length = get_packet_length(packet_id)

        if length > 0:
            # Fixed-length: read remaining bytes
            remaining = length - 1
            if remaining > 0:
                payload = await self._read_bytes(remaining, timeout)
            else:
                payload = b""
            return packet_id, id_byte + payload
        elif length == 0:
            # Variable-length: read 2-byte length, then rest
            len_bytes = await self._read_bytes(2, timeout)
            total_len = struct.unpack(">H", len_bytes)[0]
            remaining = total_len - 3  # subtract ID + length field
            if remaining > 0:
                payload = await self._read_bytes(remaining, timeout)
            else:
                payload = b""
            return packet_id, id_byte + len_bytes + payload
        else:
            # Unknown packet — read what we can and log warning
            logger.warning("unknown_packet", packet_id=f"0x{packet_id:02X}")
            return packet_id, id_byte

    async def _recv_game_packet(self, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, bytes]:
        """Receive one game-mode packet (Huffman compressed)."""
        assert self._reader is not None

        # In game mode, server sends Huffman-compressed data.
        # We need to read compressed bytes and decompress to find packet boundaries.
        # Strategy: read available data, decompress, parse packets from decompressed buffer.

        while True:
            # Try to parse a packet from the decompressed buffer
            if len(self._recv_buffer) > 0:
                packet_id = self._recv_buffer[0]
                length = get_packet_length(packet_id)

                if length > 0:
                    # Fixed-length packet
                    if len(self._recv_buffer) >= length:
                        packet_data = bytes(self._recv_buffer[:length])
                        del self._recv_buffer[:length]
                        return packet_id, packet_data
                elif length == 0:
                    # Variable-length packet
                    if len(self._recv_buffer) >= 3:
                        total_len = struct.unpack(">H", self._recv_buffer[1:3])[0]
                        if len(self._recv_buffer) >= total_len:
                            packet_data = bytes(self._recv_buffer[:total_len])
                            del self._recv_buffer[:total_len]
                            return packet_id, packet_data
                else:
                    # Unknown packet — skip 1 byte
                    logger.warning(
                        "unknown_game_packet",
                        packet_id=f"0x{packet_id:02X}",
                    )
                    del self._recv_buffer[:1]
                    continue

            # Need more data — read compressed chunk and decompress
            try:
                compressed = await asyncio.wait_for(
                    self._reader.read(4096),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                raise
            if not compressed:
                raise ConnectionError("Connection closed by server")

            # Decompress and append to buffer
            # We decompress with a generous output_len and let the buffer accumulate
            try:
                decompressed = huffman_decompress(compressed, len(compressed) * 4)
                self._recv_buffer.extend(decompressed)
            except ValueError as e:
                logger.error("huffman_error", error=str(e), compressed_len=len(compressed))
                raise

    async def recv_packet(self, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, bytes]:
        """Receive one packet (handles both login and game mode)."""
        if self._game_mode:
            return await self._recv_game_packet(timeout)
        else:
            return await self._recv_raw_packet(timeout)

    async def login(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        character_slot: int = 0,
    ) -> LoginResult:
        """Complete the full two-phase UO login flow.

        Phase 1: AccountLogin → ServerList → ServerSelect → Redirect
        Phase 2: GameLogin → SupportedFeatures → CharacterList → PlayCharacter → LoginConfirm
        """
        # === Phase 1: Account Login ===
        logger.info("login_phase1_start", host=host, port=port, username=username)
        await self._connect(host, port)

        # Send seed
        seed_data = build_seed(seed=0x01020304)
        await self._send_raw(seed_data)

        # Send AccountLogin
        login_data = build_account_login(username, password)
        await self.send_packet(login_data)

        # Receive ServerList (0xA8) or LoginDenied (0x82)
        packet_id, data = await self.recv_packet()
        if packet_id == 0x82:
            reader = PacketReader(data[1:])
            reason = reader.read_u8()
            raise ConnectionError(f"Login denied (reason={reason})")
        if packet_id != 0xA8:
            raise ConnectionError(f"Expected ServerList (0xA8), got 0x{packet_id:02X}")

        logger.info("login_server_list_received")

        # Send ServerSelect (index 0)
        select_data = build_server_select(0)
        await self.send_packet(select_data)

        # Receive ServerRedirect (0x8C)
        packet_id, data = await self.recv_packet()
        if packet_id != 0x8C:
            raise ConnectionError(f"Expected ServerRedirect (0x8C), got 0x{packet_id:02X}")

        reader = PacketReader(data[1:])  # skip packet ID
        reader.skip(4)  # redirect IP (we reconnect to same host)
        reader.skip(2)  # redirect port
        auth_key = reader.read_u32()

        logger.info(
            "login_redirect",
            auth_key=f"0x{auth_key:08X}",
        )

        # Close phase 1 connection
        await self._close()

        # === Phase 2: Game Login ===
        logger.info("login_phase2_start")
        await self._connect(host, port)

        # Send seed with auth_key
        game_seed = build_seed(seed=auth_key)
        await self._send_raw(game_seed)

        # Switch to game mode (Huffman decompression for incoming)
        self._game_mode = True

        # Send GameLogin
        game_login = build_game_login(auth_key, username, password)
        await self.send_packet(game_login)

        # Receive packets until LoginConfirm (0x1B) and LoginComplete (0x55)
        login_result: LoginResult | None = None
        got_login_complete = False
        deadline = asyncio.get_event_loop().time() + DEFAULT_TIMEOUT

        while asyncio.get_event_loop().time() < deadline:
            remaining_time = deadline - asyncio.get_event_loop().time()
            if remaining_time <= 0:
                break

            try:
                packet_id, data = await self.recv_packet(timeout=remaining_time)
            except asyncio.TimeoutError:
                break

            if packet_id == 0x82:
                reader = PacketReader(data[1:])
                reason = reader.read_u8()
                raise ConnectionError(f"Game login denied (reason={reason})")

            if packet_id == 0xA9:
                # CharacterList — send PlayCharacter
                logger.info("login_character_list_received")
                play_data = build_play_character(slot=character_slot)
                await self.send_packet(play_data)

            elif packet_id == 0x1B:
                # LoginConfirm
                reader = PacketReader(data[1:])
                serial = reader.read_u32()
                reader.skip(4)  # unknown
                body = reader.read_u16()
                x = reader.read_u16()
                y = reader.read_u16()
                reader.skip(1)  # unknown
                z = reader.read_i8()
                reader.skip(1)  # unknown
                direction = reader.read_u8() & 0x07

                login_result = LoginResult(
                    serial=serial,
                    x=x,
                    y=y,
                    z=z,
                    direction=direction,
                    body=body,
                )
                logger.info(
                    "login_confirmed",
                    serial=f"0x{serial:08X}",
                    position=f"({x}, {y}, {z})",
                    body=f"0x{body:04X}",
                )

            elif packet_id == 0x55:
                # LoginComplete
                got_login_complete = True
                logger.info("login_complete")
                break

            elif packet_id == 0xB9:
                # SupportedFeatures — log and continue
                reader = PacketReader(data[1:])
                flags = reader.read_u32() if len(data) > 5 else reader.read_u16()
                logger.debug("supported_features", flags=f"0x{flags:04X}")

            else:
                logger.debug(
                    "login_packet_ignored",
                    packet_id=f"0x{packet_id:02X}",
                    size=len(data),
                )

        if login_result is None:
            raise ConnectionError("Did not receive LoginConfirm (0x1B)")
        if not got_login_complete:
            logger.warning("login_complete_not_received")

        return login_result
