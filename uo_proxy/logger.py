"""Async JSONL logger for proxy sessions.

Each packet becomes one line. Design goals:
- Never block the forwarding loop (put_nowait + background flusher).
- Never raise into the caller — logging failures are invisible to the wire path.
- Simple schema v1; trajectory post-processing will enrich later.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

__all__ = ["ProxyLogger"]


class ProxyLogger:
    SCHEMA = "uo_proxy.packet.v1"

    def __init__(self, out_path: Path, queue_size: int = 10_000) -> None:
        self._out_path = Path(out_path)
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=queue_size
        )
        self._task: asyncio.Task[None] | None = None
        self._dropped = 0

    async def start(self) -> None:
        self._out_path.parent.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)  # sentinel
        await self._task
        self._task = None

    def record(
        self,
        session_id: str,
        direction: str,
        pid: int,
        payload: bytes,
        phase: str,
        note: str | None = None,
    ) -> None:
        event = {
            "schema": self.SCHEMA,
            "ts": time.time(),
            "session_id": session_id,
            "direction": direction,       # "C->S" | "S->C"
            "phase": phase,                # "login" | "game"
            "pid": f"0x{pid:02X}",
            "size": len(payload),
            "hex": payload.hex(),
            "note": note,
        }
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1

    @property
    def dropped(self) -> int:
        return self._dropped

    async def _flush_loop(self) -> None:
        # Simple batch: write every 500ms or whenever the queue drains.
        pending: list[dict[str, Any]] = []
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                item = "tick"

            if item is None:  # sentinel
                await self._flush(pending)
                return
            if item != "tick":
                assert isinstance(item, dict)
                pending.append(item)

            if len(pending) >= 200 or item == "tick":
                await self._flush(pending)
                pending = []

    async def _flush(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        try:
            with open(self._out_path, "a", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev, separators=(",", ":")) + "\n")
        except Exception:
            pass  # intentional: never raise into the wire path
