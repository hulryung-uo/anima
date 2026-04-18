"""Packet rewrites — the one place we modify bytes in transit.

Currently handles the login-server ServerRedirect packet (0x8C) so ClassicUO
reconnects to the proxy instead of directly to the real game server.

0x8C format (11 bytes):
  [ID=0x8C] [IP:4] [Port:u16] [AuthKey:u32]
"""

from __future__ import annotations

import ipaddress
import struct

__all__ = ["rewrite_server_redirect"]


def _pack_ipv4(addr: str) -> bytes:
    return ipaddress.IPv4Address(addr).packed


def rewrite_server_redirect(
    packet: bytes,
    new_ip: str = "127.0.0.1",
    new_port: int | None = None,
) -> bytes:
    """Rewrite the IP (and optionally port) in a 0x8C ServerRedirect packet.

    `new_port=None` keeps the original port — useful when the proxy listens
    on the same port as the upstream game server.
    """
    if len(packet) != 11 or packet[0] != 0x8C:
        raise ValueError(
            f"Not a 0x8C ServerRedirect packet (id=0x{packet[0]:02X}, len={len(packet)})"
        )
    ip_bytes = _pack_ipv4(new_ip)
    orig_port = struct.unpack(">H", packet[5:7])[0]
    port = orig_port if new_port is None else new_port
    auth_key = packet[7:11]
    return bytes([0x8C]) + ip_bytes + struct.pack(">H", port) + auth_key


def parse_server_redirect(packet: bytes) -> tuple[str, int, int]:
    """Return (ip, port, auth_key) from a 0x8C packet — used by the proxy
    so a dynamic upstream can be picked for the follow-up game connection."""
    if len(packet) != 11 or packet[0] != 0x8C:
        raise ValueError(
            f"Not a 0x8C ServerRedirect packet (id=0x{packet[0]:02X}, len={len(packet)})"
        )
    ip = str(ipaddress.IPv4Address(packet[1:5]))
    port = struct.unpack(">H", packet[5:7])[0]
    auth_key = struct.unpack(">I", packet[7:11])[0]
    return ip, port, auth_key
