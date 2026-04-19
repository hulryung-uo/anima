"""Out-of-band intent input — tail a file for new lines.

Why a file watcher: the chat-prefix capture path is convenient but goes
through the server (and other players can see it). This module gives the
user a fully private side-channel: appending a line to a plain text file
produces an intent event with the current timestamp.

Usage (from a shell, any time):
    echo "mining bootstrap" >> data/intents/input.txt

The proxy picks it up within ~1 second via `IntentWatcher` and records
it into the same JSONL stream the chat-prefix path writes to, with
`source: "file"`.

Design:
- Poll the file every `poll_interval` seconds.
- Track byte offset; re-opening a truncated file starts from 0.
- Blank lines and lines starting with `#` are skipped (comment support).
- Failure to read is logged once and retried — never raises into the
  proxy's wire path.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from .intent import IntentEvent, IntentLogger


class IntentWatcher:
    def __init__(
        self,
        path: Path,
        intent_logger: IntentLogger,
        session_id: str,
        poll_interval: float = 1.0,
    ) -> None:
        self._path = Path(path)
        self._intent_logger = intent_logger
        self._session_id = session_id
        self._poll = poll_interval
        self._offset = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        # Seek to EOF on first run so pre-existing content isn't replayed.
        try:
            if self._path.exists():
                self._offset = self._path.stat().st_size
            else:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.touch()
                self._offset = 0
        except Exception:
            self._offset = 0
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._scan_once()
            except Exception:
                pass  # keep polling regardless
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll)
                return
            except asyncio.TimeoutError:
                continue

    async def _scan_once(self) -> None:
        try:
            st = self._path.stat()
        except FileNotFoundError:
            self._offset = 0
            return
        if st.st_size < self._offset:
            # File was truncated or rotated — start over.
            self._offset = 0
        if st.st_size == self._offset:
            return
        try:
            with open(self._path, "rb") as f:
                f.seek(self._offset)
                data = f.read()
                self._offset = f.tell()
        except Exception:
            return
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return
        for line in text.splitlines():
            label = line.strip()
            if not label or label.startswith("#"):
                continue
            self._intent_logger.record(IntentEvent(
                ts=time.time(),
                session_id=self._session_id,
                label=label,
                source="file",
            ))
