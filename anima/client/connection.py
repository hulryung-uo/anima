"""UO network connection with two-phase login flow."""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from anima.perception import Perception

from anima.client.appearance import (
    TEMPLATES,
    CharacterAppearance,
    build_create_character,
)
from anima.client.codec import PacketReader, huffman_decompress_one
from anima.client.handler import PacketHandler
from anima.client.packets import (
    build_account_login,
    build_client_version,
    build_delete_character,
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

    def __init__(self, timeout: float = 10.0) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._game_mode = False
        self._recv_buffer = bytearray()  # decompressed packet bytes
        self._compressed_buffer = bytearray()  # raw compressed bytes from TCP
        self._timeout = timeout

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def _connect(self, host: str, port: int) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=self._timeout,
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

    async def _read_bytes(self, n: int, timeout: float = 0) -> bytes:
        """Read exactly n bytes from the socket."""
        timeout = timeout or self._timeout
        assert self._reader is not None
        data = await asyncio.wait_for(self._reader.readexactly(n), timeout=timeout)
        return data

    async def _recv_raw_packet(self, timeout: float = 0) -> tuple[int, bytes]:
        """Receive one raw (uncompressed) packet. Returns (packet_id, full_packet_bytes)."""
        timeout = timeout or self._timeout
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

    async def _recv_game_packet(self, timeout: float = 0) -> tuple[int, bytes]:
        """Receive one game-mode packet (Huffman compressed).

        Each server packet is independently Huffman compressed with its own
        terminal symbol. Compressed data for a single packet may span multiple
        TCP reads, so we maintain a compressed buffer across calls.
        """
        timeout = timeout or self._timeout
        assert self._reader is not None

        while True:
            # Try to parse a complete packet from the decompressed buffer
            if len(self._recv_buffer) > 0:
                packet_id = self._recv_buffer[0]
                length = get_packet_length(packet_id)

                if length > 0:
                    if len(self._recv_buffer) >= length:
                        packet_data = bytes(self._recv_buffer[:length])
                        del self._recv_buffer[:length]
                        return packet_id, packet_data
                elif length == 0:
                    if len(self._recv_buffer) >= 3:
                        total_len = struct.unpack(">H", self._recv_buffer[1:3])[0]
                        if total_len >= 3 and len(self._recv_buffer) >= total_len:
                            packet_data = bytes(self._recv_buffer[:total_len])
                            del self._recv_buffer[:total_len]
                            return packet_id, packet_data
                else:
                    logger.warning(
                        "unknown_game_packet",
                        packet_id=f"0x{packet_id:02X}",
                    )
                    del self._recv_buffer[:1]
                    continue

            # Try to decompress one packet from the compressed buffer
            if len(self._compressed_buffer) > 0:
                decompressed, consumed = huffman_decompress_one(bytes(self._compressed_buffer))
                if decompressed is not None and consumed > 0:
                    del self._compressed_buffer[:consumed]
                    self._recv_buffer.extend(decompressed)
                    continue  # go back and try to parse
                # decompressed is None → need more TCP data to complete this packet

            # Need more data from TCP
            try:
                data = await asyncio.wait_for(
                    self._reader.read(4096),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                raise
            if not data:
                raise ConnectionError("Connection closed by server")

            self._compressed_buffer.extend(data)

    async def recv_packet(self, timeout: float = 0) -> tuple[int, bytes]:
        """Receive one packet (handles both login and game mode)."""
        timeout = timeout or self._timeout
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
        character_name: str = "",
        character_template: str = "random",
        character_city: int = 3,
        delete_existing: bool = False,
        packet_handler: PacketHandler | None = None,
        perception: Perception | None = None,
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
        play_sent = False
        delete_sent = False
        deadline = asyncio.get_event_loop().time() + self._timeout

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

            if packet_id in (0xA9, 0x86) and not play_sent:
                # CharacterList — parse slots to find a character
                reader = PacketReader(data[3:])  # skip id + length
                char_count = reader.read_u8()
                char_name = ""
                char_slot = character_slot
                for i in range(char_count):
                    name = reader.read_ascii(30)
                    reader.skip(30)  # password field
                    if i == character_slot and name:
                        char_name = name
                    elif not char_name and name:
                        char_name = name
                        char_slot = i

                if char_name and delete_existing and not delete_sent:
                    # Delete existing character first
                    delete_sent = True
                    logger.info(
                        "login_deleting_character",
                        character=char_name,
                        slot=char_slot,
                    )
                    del_data = build_delete_character(
                        password,
                        char_slot,
                    )
                    await self.send_packet(del_data)
                    # Server will re-send char list (0x86)
                    char_name = ""
                    continue

                if char_name:
                    logger.info(
                        "login_playing_character",
                        character=char_name,
                        slot=char_slot,
                    )
                    play_data = build_play_character(
                        name=char_name,
                        slot=char_slot,
                    )
                    await self.send_packet(play_data)
                else:
                    # No characters — create one
                    name = character_name or "Anima"
                    if character_template in TEMPLATES:
                        appearance = TEMPLATES[character_template]
                        appearance.name = name
                        appearance.city_index = character_city
                    else:
                        appearance = CharacterAppearance.random(
                            name=name,
                            city_index=character_city,
                        )
                    logger.info(
                        "login_creating_character",
                        name=appearance.name,
                        female=appearance.female,
                        city=character_city,
                    )
                    create_data = build_create_character(appearance, slot=0)
                    await self.send_packet(create_data)
                play_sent = True

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

                # Sync perception immediately so handlers have correct serial
                if perception is not None:
                    perception.self_state.serial = serial
                    perception.self_state.x = x
                    perception.self_state.y = y
                    perception.self_state.z = z
                    perception.self_state.direction = direction
                    perception.self_state.body = body

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

            elif packet_id == 0xBD:
                # ClientVersion request — respond with our version
                await self.send_packet(build_client_version("7.0.102.3"))
                logger.debug("client_version_sent")

            elif packet_id == 0xB9:
                # SupportedFeatures — log and continue
                reader = PacketReader(data[1:])
                flags = reader.read_u32() if len(data) > 5 else reader.read_u16()
                logger.debug("supported_features", flags=f"0x{flags:04X}")

            elif login_result is not None and packet_handler is not None:
                # After LoginConfirm, dispatch world-state packets
                # through the handler instead of ignoring them
                if not packet_handler.dispatch(packet_id, data):
                    logger.debug(
                        "login_packet_unhandled",
                        packet_id=f"0x{packet_id:02X}",
                        size=len(data),
                    )

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
