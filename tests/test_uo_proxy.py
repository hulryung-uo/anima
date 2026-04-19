"""Tests for uo_proxy: framing, rewrite, logger."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import struct
from pathlib import Path

import pytest

from uo_proxy.framing import FrameError, frame_one, iter_packets
from uo_proxy.intent import (
    IntentEvent,
    IntentLogger,
    build_plain_unicode_speech,
    extract_intent_from_speech,
)
from uo_proxy.intent_watch import IntentWatcher
from uo_proxy.logger import ProxyLogger
from uo_proxy.rewrite import (
    parse_server_redirect,
    rewrite_server_redirect,
)

# ------------------------------------------------------------------ framing


class TestFraming:
    def test_fixed_length_packet(self):
        # 0x22 = ConfirmWalk, 3 bytes fixed
        packet = bytes([0x22, 0x01, 0x00])
        result = frame_one(packet)
        assert result == (packet, 3)

    def test_incomplete_fixed(self):
        assert frame_one(bytes([0x22, 0x01])) is None

    def test_variable_length_packet(self):
        # 0x03 = ASCII speech, variable
        payload = b"hello\x00"
        total = 3 + len(payload)  # id + len + payload
        packet = bytes([0x03]) + struct.pack(">H", total) + payload
        assert frame_one(packet) == (packet, total)

    def test_incomplete_variable_header(self):
        assert frame_one(bytes([0x03, 0x00])) is None

    def test_incomplete_variable_body(self):
        total = 20
        buf = bytes([0x03]) + struct.pack(">H", total) + b"AB"
        assert frame_one(buf) is None

    def test_unknown_packet_raises(self):
        with pytest.raises(FrameError):
            frame_one(bytes([0xFE, 0, 0]))

    def test_iter_packets_consumes_in_place(self):
        a = bytes([0x22, 0x01, 0x00])
        b = bytes([0x22, 0x02, 0x00])
        buf = bytearray(a + b + b"\x22")  # trailing partial
        out = list(iter_packets(buf))
        assert out == [a, b]
        assert bytes(buf) == b"\x22"


# ------------------------------------------------------------------ rewrite


class TestRewrite:
    def _make(self, ip: str = "10.0.0.1", port: int = 2593, auth: int = 0xDEADBEEF) -> bytes:
        return (
            bytes([0x8C])
            + ipaddress.IPv4Address(ip).packed
            + struct.pack(">H", port)
            + struct.pack(">I", auth)
        )

    def test_parse_roundtrip(self):
        pkt = self._make("192.168.1.50", 7777, 0xCAFEBABE)
        assert parse_server_redirect(pkt) == ("192.168.1.50", 7777, 0xCAFEBABE)

    def test_rewrite_preserves_port_and_auth(self):
        pkt = self._make("10.0.0.1", 2593, 0xDEADBEEF)
        out = rewrite_server_redirect(pkt, new_ip="127.0.0.1")
        ip, port, auth = parse_server_redirect(out)
        assert ip == "127.0.0.1"
        assert port == 2593
        assert auth == 0xDEADBEEF

    def test_rewrite_changes_port(self):
        pkt = self._make("10.0.0.1", 2593, 0xDEADBEEF)
        out = rewrite_server_redirect(pkt, new_ip="127.0.0.1", new_port=2594)
        ip, port, auth = parse_server_redirect(out)
        assert (ip, port, auth) == ("127.0.0.1", 2594, 0xDEADBEEF)

    def test_rewrite_rejects_wrong_packet(self):
        with pytest.raises(ValueError):
            rewrite_server_redirect(bytes([0x22, 0, 0]))


# ------------------------------------------------------------------ logger


class TestProxyLogger:
    @pytest.mark.asyncio
    async def test_writes_events_to_jsonl(self, tmp_path: Path):
        out = tmp_path / "demo.jsonl"
        logger = ProxyLogger(out)
        await logger.start()
        try:
            logger.record(
                session_id="sess1", direction="C->S", pid=0x02,
                payload=b"\x02\x01\x02\x03\x04\x05\x06", phase="game",
                note=None,
            )
            logger.record(
                session_id="sess1", direction="S->C", pid=0x22,
                payload=b"\x22\x01\x00", phase="game",
                note="confirm_walk",
            )
            # Give the flusher a moment
            await asyncio.sleep(0.7)
        finally:
            await logger.stop()

        lines = out.read_text().strip().splitlines()
        assert len(lines) == 2
        e1 = json.loads(lines[0])
        assert e1["schema"] == "uo_proxy.packet.v1"
        assert e1["direction"] == "C->S"
        assert e1["pid"] == "0x02"
        assert e1["size"] == 7
        assert "hex" in e1
        e2 = json.loads(lines[1])
        assert e2["note"] == "confirm_walk"

    @pytest.mark.asyncio
    async def test_dropped_counter_when_queue_full(self, tmp_path: Path):
        out = tmp_path / "demo.jsonl"
        logger = ProxyLogger(out, queue_size=2)
        # Don't start the flusher — queue fills.
        for _ in range(10):
            logger.record(
                session_id="x", direction="C->S", pid=0x02,
                payload=b"\x02", phase="login", note=None,
            )
        assert logger.dropped > 0


# ------------------------------------------------------------------ intent


class TestIntentExtraction:
    def test_prefix_hit_unicode_speech(self):
        pkt = build_plain_unicode_speech("//mining bootstrap")
        label, drop = extract_intent_from_speech(pkt, prefix="//")
        assert drop is True
        assert label == "mining bootstrap"

    def test_prefix_miss_unicode_speech(self):
        pkt = build_plain_unicode_speech("hello world")
        label, drop = extract_intent_from_speech(pkt, prefix="//")
        assert (label, drop) == (None, False)

    def test_prefix_hit_korean_text(self):
        pkt = build_plain_unicode_speech("//광석 채굴 시작")
        label, drop = extract_intent_from_speech(pkt, prefix="//")
        assert drop is True
        assert label == "광석 채굴 시작"

    def test_empty_prefix_only_drops_without_label(self):
        pkt = build_plain_unicode_speech("// ")
        label, drop = extract_intent_from_speech(pkt, prefix="//")
        assert drop is True
        assert label is None

    def test_non_speech_packet_passes_through(self):
        # 0x22 ConfirmWalk — totally unrelated
        pkt = bytes([0x22, 0x01, 0x00])
        label, drop = extract_intent_from_speech(pkt, prefix="//")
        assert (label, drop) == (None, False)

    def test_custom_prefix(self):
        pkt = build_plain_unicode_speech(";;mine")
        label, drop = extract_intent_from_speech(pkt, prefix=";;")
        assert drop is True and label == "mine"

    def test_ascii_speech_0x03(self):
        # Build a minimal 0x03 packet: [ID][len:u16][type][hue:u16][font:u16][text\0]
        text = b"//sell to vendor\x00"
        body = bytes([0]) + b"\x00" * 4 + text  # type, hue, font all zero
        total = 3 + len(body)
        pkt = bytes([0x03]) + struct.pack(">H", total) + body
        label, drop = extract_intent_from_speech(pkt, prefix="//")
        assert drop is True
        assert label == "sell to vendor"


class TestIntentLogger:
    def test_record_writes_jsonl(self, tmp_path: Path):
        out = tmp_path / "intents.jsonl"
        log = IntentLogger(out)
        log.record(IntentEvent(
            ts=1234567890.0, session_id="sess1",
            label="mining bootstrap", source="chat",
        ))
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["schema"] == "uo_proxy.intent.v1"
        assert entry["label"] == "mining bootstrap"
        assert entry["source"] == "chat"

    def test_record_preserves_unicode(self, tmp_path: Path):
        out = tmp_path / "intents.jsonl"
        log = IntentLogger(out)
        log.record(IntentEvent(
            ts=0.0, session_id="s", label="광석", source="chat",
        ))
        entry = json.loads(out.read_text().strip())
        assert entry["label"] == "광석"


class TestIntentWatcher:
    @pytest.mark.asyncio
    async def test_appended_lines_become_intent_events(self, tmp_path: Path):
        input_file = tmp_path / "input.txt"
        intents_out = tmp_path / "intents.jsonl"
        intent_log = IntentLogger(intents_out)
        watcher = IntentWatcher(
            path=input_file, intent_logger=intent_log,
            session_id="sess", poll_interval=0.05,
        )
        await watcher.start()
        try:
            # Pre-existing content written AFTER start shouldn't be missed.
            with open(input_file, "a", encoding="utf-8") as f:
                f.write("mining bootstrap\n")
                f.write("# comment — should be skipped\n")
                f.write("\n")
                f.write("광석 채굴\n")
            await asyncio.sleep(0.3)
        finally:
            await watcher.stop()

        lines = intents_out.read_text().strip().splitlines()
        entries = [json.loads(ln) for ln in lines]
        labels = [e["label"] for e in entries]
        assert labels == ["mining bootstrap", "광석 채굴"]
        assert all(e["source"] == "file" for e in entries)

    @pytest.mark.asyncio
    async def test_skips_preexisting_content_on_start(self, tmp_path: Path):
        input_file = tmp_path / "input.txt"
        input_file.write_text("old line\n")
        intents_out = tmp_path / "intents.jsonl"
        intent_log = IntentLogger(intents_out)
        watcher = IntentWatcher(
            path=input_file, intent_logger=intent_log,
            session_id="sess", poll_interval=0.05,
        )
        await watcher.start()
        await asyncio.sleep(0.15)
        with open(input_file, "a", encoding="utf-8") as f:
            f.write("new line\n")
        await asyncio.sleep(0.15)
        await watcher.stop()

        entries = [json.loads(ln) for ln in intents_out.read_text().splitlines()]
        assert [e["label"] for e in entries] == ["new line"]


# ------------------------------------------------------------------ integration


class TestProxyIntegration:
    @pytest.mark.asyncio
    async def test_login_phase_rewrites_redirect_and_logs(self, tmp_path: Path):
        """End-to-end: mock upstream replies with 0x8C, proxy rewrites IP,
        client sees rewritten packet, logger captures both directions."""
        from uo_proxy.proxy import ProxyConfig

        # 1. Mock upstream server: sends one 0x8C redirect to any connector.
        orig_redirect = (
            bytes([0x8C])
            + ipaddress.IPv4Address("8.8.8.8").packed
            + struct.pack(">H", 2594)
            + struct.pack(">I", 0xDEADBEEF)
        )
        received_from_client: list[bytes] = []

        async def upstream_handler(r: asyncio.StreamReader, w: asyncio.StreamWriter):
            # Expect seed (21 bytes) then one framed packet
            seed = await r.readexactly(21)
            received_from_client.append(seed)
            # read first framed packet (0x80 AccountLogin, 62 bytes)
            pkt = await r.readexactly(62)
            received_from_client.append(pkt)
            # send redirect
            w.write(orig_redirect)
            await w.drain()
            await asyncio.sleep(0.05)
            w.close()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        up_port = upstream.sockets[0].getsockname()[1]

        # 2. Start proxy pointing at mock upstream.
        out = tmp_path / "session.jsonl"
        logger = ProxyLogger(out)
        config = ProxyConfig(
            listen_host="127.0.0.1", listen_port=0,
            upstream_host="127.0.0.1", upstream_port=up_port,
            advertised_host="127.0.0.1", advertised_port=9999,
        )
        await logger.start()

        async def handle(r, w):
            from uo_proxy.proxy import Session
            sess = Session(config, logger)
            await sess.run(r, w)

        proxy_server = await asyncio.start_server(handle, "127.0.0.1", 0)
        proxy_port = proxy_server.sockets[0].getsockname()[1]

        # 3. Act as ClassicUO: connect, send seed + 0x80, read redirect.
        client_r, client_w = await asyncio.open_connection("127.0.0.1", proxy_port)
        seed = bytes([0xEF]) + b"\x00" * 20
        client_w.write(seed)
        # 0x80 AccountLogin: 62 bytes fixed — any 62 bytes work for this test.
        login = bytes([0x80]) + b"\x00" * 61
        client_w.write(login)
        await client_w.drain()

        rewritten = await asyncio.wait_for(
            client_r.readexactly(11), timeout=2.0,
        )

        # 4. Assertions: proxy forwarded bytes + rewrote redirect.
        assert received_from_client[0] == seed
        assert received_from_client[1] == login
        assert rewritten[0] == 0x8C
        ip = str(ipaddress.IPv4Address(rewritten[1:5]))
        port = struct.unpack(">H", rewritten[5:7])[0]
        auth = struct.unpack(">I", rewritten[7:11])[0]
        assert ip == "127.0.0.1"
        assert port == 9999
        assert auth == 0xDEADBEEF

        client_w.close()
        try:
            await asyncio.wait_for(client_w.wait_closed(), timeout=1.0)
        except Exception:
            pass

        # Give session cleanup a moment
        await asyncio.sleep(0.2)

        # 5. Logger saw seed, login, and redirect entries.
        await logger.stop()
        proxy_server.close()
        upstream.close()
        try:
            await asyncio.wait_for(proxy_server.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        try:
            await asyncio.wait_for(upstream.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

        lines = out.read_text().strip().splitlines()
        events = [json.loads(line) for line in lines]
        dirs = [(e["direction"], e["pid"]) for e in events]
        assert ("C->S", "0xEF") in dirs        # seed
        assert ("C->S", "0x80") in dirs        # login
        # redirect is logged with a 'redirect' note
        redirect_events = [e for e in events if e["pid"] == "0x8C"]
        assert redirect_events
        assert redirect_events[0]["note"] and "redirect" in redirect_events[0]["note"]

    @pytest.mark.asyncio
    async def test_unknown_cs_pid_does_not_tear_down_session(self, tmp_path: Path):
        """Packet IDs missing from PACKET_LENGTHS must be passed through
        one byte at a time so the game connection survives an incomplete
        length table."""
        from uo_proxy.proxy import ProxyConfig, Session

        up_received: list[bytes] = []

        async def upstream_handler(r: asyncio.StreamReader, w: asyncio.StreamWriter):
            try:
                while True:
                    data = await r.read(4096)
                    if not data:
                        return
                    up_received.append(data)
            except Exception:
                return

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        up_port = upstream.sockets[0].getsockname()[1]

        out = tmp_path / "session.jsonl"
        logger = ProxyLogger(out)
        await logger.start()
        config = ProxyConfig(
            listen_host="127.0.0.1", listen_port=0,
            upstream_host="127.0.0.1", upstream_port=up_port,
            intent_prefix="",
        )

        async def handle(r, w):
            sess = Session(config, logger)
            await sess.run(r, w)

        proxy_server = await asyncio.start_server(handle, "127.0.0.1", 0)
        proxy_port = proxy_server.sockets[0].getsockname()[1]

        # Phase 2 raw seed, then an unknown pid 0xFE (absent from table),
        # then a valid 0x73 ping (2 bytes) to verify resync.
        cr, cw = await asyncio.open_connection("127.0.0.1", proxy_port)
        cw.write(bytes([0x9C, 0x69, 0xB5, 0x8C]))  # raw seed
        cw.write(bytes([0xFE]))                     # single unknown byte
        cw.write(bytes([0x73, 0x00]))               # ping
        await cw.drain()
        await asyncio.sleep(0.2)

        cw.close()
        try:
            await asyncio.wait_for(cw.wait_closed(), timeout=1.0)
        except Exception:
            pass
        await asyncio.sleep(0.2)
        await logger.stop()
        proxy_server.close()
        upstream.close()
        try:
            await asyncio.wait_for(proxy_server.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        try:
            await asyncio.wait_for(upstream.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

        # Upstream must receive seed + all subsequent bytes in order.
        up_bytes = b"".join(up_received)
        assert up_bytes.startswith(
            bytes([0x9C, 0x69, 0xB5, 0x8C, 0xFE, 0x73, 0x00])
        )

        events = [json.loads(line) for line in out.read_text().splitlines()]
        notes = [e.get("note") for e in events]
        # 0xFE byte should have been logged as "unknown_pid_log" on the
        # parser side, not raised a FrameError that closed the session.
        assert any(n and "unknown_pid_log 0xFE" in n for n in notes)
        # 0x73 ping still logged normally afterwards
        assert any(
            e["pid"] == "0x73" and e["direction"] == "C->S" for e in events
        )

    @pytest.mark.asyncio
    async def test_phase2_raw_4byte_seed_is_forwarded_correctly(self, tmp_path: Path):
        """When client opens a new connection and sends a 4-byte raw seed
        (phase-2 reconnect pattern used by ClassicUO), the proxy must read
        only 4 bytes, switch to game mode, and then forward the following
        0x91 GameLogin intact."""
        from uo_proxy.proxy import ProxyConfig, Session

        up_received: list[bytes] = []

        async def upstream_handler(r: asyncio.StreamReader, w: asyncio.StreamWriter):
            try:
                while True:
                    data = await r.read(4096)
                    if not data:
                        return
                    up_received.append(data)
            except Exception:
                return

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        up_port = upstream.sockets[0].getsockname()[1]

        out = tmp_path / "session.jsonl"
        logger = ProxyLogger(out)
        await logger.start()
        config = ProxyConfig(
            listen_host="127.0.0.1", listen_port=0,
            upstream_host="127.0.0.1", upstream_port=up_port,
            intent_prefix="",
        )

        async def handle(r, w):
            sess = Session(config, logger)
            await sess.run(r, w)

        proxy_server = await asyncio.start_server(handle, "127.0.0.1", 0)
        proxy_port = proxy_server.sockets[0].getsockname()[1]

        # Client sends 4-byte raw seed + full 65-byte 0x91 GameLogin.
        raw_seed = bytes([0x9C, 0x69, 0xB5, 0x8C])  # example auth_key
        game_login = bytes([0x91]) + b"\x00" * 64
        cr, cw = await asyncio.open_connection("127.0.0.1", proxy_port)
        cw.write(raw_seed + game_login)
        await cw.drain()
        await asyncio.sleep(0.2)

        cw.close()
        try:
            await asyncio.wait_for(cw.wait_closed(), timeout=1.0)
        except Exception:
            pass
        await asyncio.sleep(0.2)
        await logger.stop()
        proxy_server.close()
        upstream.close()
        try:
            await asyncio.wait_for(proxy_server.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        try:
            await asyncio.wait_for(upstream.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

        up_bytes = b"".join(up_received)
        # Upstream must receive seed + full 65 bytes, in order, no truncation.
        assert up_bytes.startswith(raw_seed + game_login)

        events = [json.loads(line) for line in out.read_text().splitlines()]
        kinds = [(e["direction"], e["pid"], e.get("note")) for e in events]
        # Seed logged as raw4, followed by 0x91 in game phase.
        assert any(k[2] == "seed_raw4" for k in kinds)
        assert any(e["pid"] == "0x91" and e["phase"] == "game" for e in events)

    @pytest.mark.asyncio
    async def test_chat_prefix_is_captured_as_intent(self, tmp_path: Path):
        """Client chat line starting with '//' is captured as an intent
        label. The wire-first design forwards the packet unchanged so
        forwarding never stalls; the user's chat is still visible to other
        players until a more resilient framer is implemented."""
        from uo_proxy.proxy import ProxyConfig, Session

        received_by_upstream: list[bytes] = []

        async def upstream_handler(r: asyncio.StreamReader, w: asyncio.StreamWriter):
            try:
                while True:
                    data = await r.read(4096)
                    if not data:
                        return
                    received_by_upstream.append(data)
            except Exception:
                return

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        up_port = upstream.sockets[0].getsockname()[1]

        out = tmp_path / "session.jsonl"
        intent_out = tmp_path / "intents.jsonl"
        logger = ProxyLogger(out)
        intent_logger = IntentLogger(intent_out)
        config = ProxyConfig(
            listen_host="127.0.0.1", listen_port=0,
            upstream_host="127.0.0.1", upstream_port=up_port,
            intent_prefix="//",
        )
        await logger.start()

        async def handle(r, w):
            sess = Session(config, logger, intent_logger=intent_logger)
            await sess.run(r, w)

        proxy_server = await asyncio.start_server(handle, "127.0.0.1", 0)
        proxy_port = proxy_server.sockets[0].getsockname()[1]

        # Send seed, a 0x91 GameLogin to switch to game phase (just to
        # exercise a realistic path), then an intent chat line.
        cr, cw = await asyncio.open_connection("127.0.0.1", proxy_port)
        cw.write(bytes([0xEF]) + b"\x00" * 20)                  # seed
        cw.write(bytes([0x91]) + b"\x00" * 64)                  # game login (65 bytes)
        intent_pkt = build_plain_unicode_speech("//mining bootstrap")
        cw.write(intent_pkt)
        # Also a non-intent speech line that SHOULD be forwarded
        normal_pkt = build_plain_unicode_speech("hi there")
        cw.write(normal_pkt)
        await cw.drain()
        await asyncio.sleep(0.2)

        cw.close()
        try:
            await asyncio.wait_for(cw.wait_closed(), timeout=1.0)
        except Exception:
            pass
        await asyncio.sleep(0.2)

        await logger.stop()
        proxy_server.close()
        upstream.close()
        try:
            await asyncio.wait_for(proxy_server.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        try:
            await asyncio.wait_for(upstream.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

        # Upstream receives both packets — wire-first forwarding means we
        # no longer drop the intent packet. (The label is still captured.)
        up_bytes = b"".join(received_by_upstream)
        assert normal_pkt in up_bytes
        assert intent_pkt in up_bytes

        # Intent logger captured the label.
        intent_lines = intent_out.read_text().strip().splitlines()
        assert len(intent_lines) == 1
        entry = json.loads(intent_lines[0])
        assert entry["label"] == "mining bootstrap"
        assert entry["source"] == "chat"
