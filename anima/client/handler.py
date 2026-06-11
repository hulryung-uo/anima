"""Packet handler dispatch registry."""

from __future__ import annotations

from collections.abc import Callable

import structlog

logger = structlog.get_logger()

# Handler signature: (packet_id: int, data: bytes) -> None
PacketHandlerFunc = Callable[[int, bytes], None]


class PacketHandler:
    """Registry that maps packet IDs to handler functions."""

    def __init__(self) -> None:
        self._handlers: dict[int, PacketHandlerFunc] = {}

    def register(self, packet_id: int, func: PacketHandlerFunc) -> None:
        self._handlers[packet_id] = func

    def dispatch(self, packet_id: int, data: bytes) -> bool:
        """Dispatch a packet to its handler. Returns True if handled."""
        handler = self._handlers.get(packet_id)
        if handler is not None:
            try:
                handler(packet_id, data)
            except Exception as e:
                # One malformed packet must never kill the session (a 0x0B
                # mis-parse used to crash the agent on every combat hit).
                logger.error(
                    "packet_handler_error",
                    packet_id=f"0x{packet_id:02X}",
                    size=len(data),
                    error=str(e),
                )
            return True
        return False

    def has_handler(self, packet_id: int) -> bool:
        return packet_id in self._handlers
