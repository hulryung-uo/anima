"""Core proxy session: shuttle bytes between ClassicUO and upstream,
log framed packets, handle Huffman on the server→client side, and rewrite
the 0x8C ServerRedirect so the client reconnects back to the proxy.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

from anima.client.codec import huffman_decompress_one

from .framing import FrameError, frame_one
from .logger import ProxyLogger
from .rewrite import parse_server_redirect, rewrite_server_redirect

# Client sends a 21-byte Seed (0xEF) before any framed packet. Treat it as
# a prelude we forward as-is.
_SEED_LEN = 21


@dataclass
class ProxyConfig:
    listen_host: str = "127.0.0.1"
    listen_port: int = 2593
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 2593
    # IP/port to advertise to the client in 0x8C rewrites. None=same as listen.
    advertised_host: str | None = None
    advertised_port: int | None = None


class Session:
    """One ClassicUO↔upstream connection pair.

    State machine:
        1. Read 21-byte seed from client, forward.
        2. Read first framed packet from client:
             - 0x80 (AccountLogin) → login phase: both directions plaintext.
             - 0x91 (GameLogin)    → game phase: C→S plaintext, S→C Huffman.
        3. Forward in both directions concurrently until either side closes.
    """

    def __init__(self, config: ProxyConfig, logger: ProxyLogger) -> None:
        self.config = config
        self.logger = logger
        self.session_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
        self.phase: str = "login"   # "login" or "game"
        self._closed = False

    # ------------------------------------------------------------------ helpers

    def _log(
        self,
        direction: str,
        pid: int,
        payload: bytes,
        note: str | None = None,
    ) -> None:
        self.logger.record(
            self.session_id, direction, pid, payload, self.phase, note
        )

    async def _read_plaintext_packet(
        self, reader: asyncio.StreamReader, buf: bytearray
    ) -> bytes:
        """Read bytes until one complete framed packet is available."""
        while True:
            result = frame_one(bytes(buf))
            if result is not None:
                packet, consumed = result
                del buf[:consumed]
                return packet
            chunk = await reader.read(4096)
            if not chunk:
                raise ConnectionError("peer closed")
            buf.extend(chunk)

    # ------------------------------------------------------------------ wire loops

    async def _client_to_server(
        self,
        client_reader: asyncio.StreamReader,
        up_writer: asyncio.StreamWriter,
    ) -> None:
        """C→S is always plaintext. We parse framed packets and forward
        the exact bytes we read so that no re-serialization happens."""
        # 1. Seed (21 bytes fixed, always 0xEF in practice)
        try:
            seed = await client_reader.readexactly(_SEED_LEN)
        except asyncio.IncompleteReadError:
            return
        up_writer.write(seed)
        await up_writer.drain()
        self._log("C->S", seed[0] if seed else 0, seed, note="seed")

        buf = bytearray()
        try:
            while not self._closed:
                packet = await self._read_plaintext_packet(client_reader, buf)
                pid = packet[0]
                if pid == 0x91:
                    # Game login — subsequent S→C traffic will be Huffman.
                    self.phase = "game"
                up_writer.write(packet)
                await up_writer.drain()
                self._log("C->S", pid, packet)
        except (ConnectionError, asyncio.IncompleteReadError, FrameError) as e:
            self._log("C->S", 0, b"", note=f"closed:{type(e).__name__}")
        finally:
            await self._safe_close(up_writer)

    async def _server_to_client(
        self,
        up_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """S→C: plaintext until the client sends 0x91; after that, Huffman.

        We forward raw bytes unchanged to the client (because we have no
        Huffman compressor) and decompress internally only to log and to
        detect + rewrite the 0x8C redirect on the login side.
        """
        plaintext_buf = bytearray()
        huffman_buf = bytearray()
        decompressed_buf = bytearray()

        try:
            while not self._closed:
                if self.phase == "login":
                    # Plaintext both ways. Read → frame → maybe rewrite → forward.
                    chunk = await up_reader.read(4096)
                    if not chunk:
                        raise ConnectionError("upstream closed")
                    plaintext_buf.extend(chunk)

                    while True:
                        result = frame_one(bytes(plaintext_buf))
                        if result is None:
                            break
                        packet, consumed = result
                        del plaintext_buf[:consumed]

                        if packet[0] == 0x8C:
                            # ServerRedirect — point client back at us.
                            try:
                                orig_ip, orig_port, auth = parse_server_redirect(packet)
                                self._log(
                                    "S->C", 0x8C, packet,
                                    note=f"redirect orig={orig_ip}:{orig_port} auth=0x{auth:08X}",
                                )
                            except ValueError:
                                pass
                            rewritten = rewrite_server_redirect(
                                packet,
                                new_ip=self.config.advertised_host or self.config.listen_host,
                                new_port=self.config.advertised_port,
                            )
                            client_writer.write(rewritten)
                            await client_writer.drain()
                            # Note: the client will now DISCONNECT and
                            # open a fresh TCP connection to us. That new
                            # connection becomes a new Session.
                        else:
                            client_writer.write(packet)
                            await client_writer.drain()
                            self._log("S->C", packet[0], packet)
                else:
                    # Game phase: forward raw compressed bytes, log decompressed.
                    chunk = await up_reader.read(4096)
                    if not chunk:
                        raise ConnectionError("upstream closed")
                    # Pass through untouched — we don't have a Huffman encoder.
                    client_writer.write(chunk)
                    await client_writer.drain()

                    huffman_buf.extend(chunk)
                    self._decode_huffman_for_log(huffman_buf, decompressed_buf)
        except (ConnectionError, asyncio.IncompleteReadError, FrameError) as e:
            self._log("S->C", 0, b"", note=f"closed:{type(e).__name__}")
        finally:
            await self._safe_close(client_writer)

    def _decode_huffman_for_log(
        self, huffman_buf: bytearray, decoded_buf: bytearray
    ) -> None:
        """Decompress whatever Huffman packets are complete, frame them,
        and log. Leftover compressed / uncompressed bytes stay buffered."""
        # 1. Decompress as many complete Huffman packets as we can.
        while huffman_buf:
            result = huffman_decompress_one(bytes(huffman_buf))
            if result is None:
                return
            chunk, consumed = result
            if chunk is None or consumed == 0:
                return
            del huffman_buf[:consumed]
            decoded_buf.extend(chunk)

        # 2. Frame the decompressed stream into packets and log each.
        while decoded_buf:
            try:
                framed = frame_one(bytes(decoded_buf))
            except FrameError:
                # Desync — drop one byte and keep trying. Imperfect but rare
                # and never affects the wire path (we already forwarded raw).
                del decoded_buf[:1]
                continue
            if framed is None:
                return
            packet, consumed = framed
            del decoded_buf[:consumed]
            self._log("S->C", packet[0], packet)

    # ------------------------------------------------------------------ lifecycle

    async def _safe_close(self, writer: asyncio.StreamWriter) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def run(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection(
                self.config.upstream_host, self.config.upstream_port,
            )
        except OSError as e:
            self._log("S->C", 0, b"", note=f"upstream_connect_failed:{e}")
            client_writer.close()
            try:
                await client_writer.wait_closed()
            except Exception:
                pass
            return

        c2s_task = asyncio.create_task(
            self._client_to_server(client_reader, up_writer)
        )
        s2c_task = asyncio.create_task(
            self._server_to_client(up_reader, client_writer)
        )
        try:
            # When either direction closes, cancel the other so this session ends.
            _, pending = await asyncio.wait(
                {c2s_task, s2c_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            self._closed = True
            for t in pending:
                t.cancel()
            for t in pending:
                try:
                    await t
                except BaseException:
                    pass
        finally:
            # Force-close both sockets so no resource is leaked even if one
            # direction's `finally` returned early due to self._closed.
            for w in (up_writer, client_writer):
                try:
                    w.close()
                except Exception:
                    pass
            for w in (up_writer, client_writer):
                try:
                    await asyncio.wait_for(w.wait_closed(), timeout=1.0)
                except BaseException:
                    pass


async def serve(config: ProxyConfig, logger: ProxyLogger) -> None:
    """Run the proxy until cancelled."""
    await logger.start()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        session = Session(config, logger)
        try:
            await session.run(reader, writer)
        except Exception as e:
            # Never let a single session crash the listener.
            session._log("S->C", 0, b"", note=f"session_error:{type(e).__name__}:{e}")

    server = await asyncio.start_server(
        handle, config.listen_host, config.listen_port,
    )
    try:
        async with server:
            await server.serve_forever()
    finally:
        await logger.stop()
