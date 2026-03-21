#!/usr/bin/env python3
"""Standalone TUI monitor -- watches agent events.jsonl in real time.

Usage:  python tools/tui_monitor.py [--events-file data/events.jsonl] [--refresh 0.5]
Keys:   q quit  f filter  p pause  c clear
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

ICONS = {
    "brain": "\u2b50", "action": "\u2692", "skill": "\u2692", "combat": "\u2694",
    "avatar": "\u2139", "movement": "\u2192", "social": "\U0001f4ac", "system": "\u2139",
}
FILTERS = ["*", "action.*", "brain.*", "avatar.*", "skill.*", "combat.*"]


@dataclass
class Event:
    ts: str = ""
    topic: str = ""
    message: str = ""
    importance: int = 1
    data: dict = field(default_factory=dict)


class FileTailer:
    """Tails a JSONL file, yielding new lines as they appear."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._pos: int = 0

    def seek_tail(self, n: int = 50) -> None:
        if not self._path.exists():
            return
        size = self._path.stat().st_size
        chunk = min(size, n * 300)
        with open(self._path, "rb") as f:
            f.seek(max(0, size - chunk))
            data = f.read()
        lines = data.split(b"\n")
        keep = min(n, len(lines))
        skip = sum(len(ln) + 1 for ln in lines[:-keep]) if keep < len(lines) else 0
        self._pos = max(0, size - chunk) + skip

    def read_new(self) -> list[Event]:
        if not self._path.exists():
            return []
        try:
            with open(self._path, "r") as f:
                f.seek(self._pos)
                raw = f.read()
                self._pos = f.tell()
        except OSError:
            return []
        events = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(Event(
                ts=obj.get("ts", ""), topic=obj.get("topic", ""),
                message=obj.get("message", ""), importance=obj.get("importance", 1),
                data=obj,
            ))
        return events


class MonitorState:
    """Holds parsed state extracted from events."""

    def __init__(self, max_events: int = 200) -> None:
        self.events: deque[Event] = deque(maxlen=max_events)
        self.skill_results: deque[Event] = deque(maxlen=30)
        self.last_position = "?"
        self.last_health = "?"
        self.event_counts: dict[str, int] = {}

    def ingest(self, events: list[Event]) -> None:
        for ev in events:
            self.events.append(ev)
            prefix = ev.topic.split(".")[0] if ev.topic else "unknown"
            self.event_counts[prefix] = self.event_counts.get(prefix, 0) + 1
            if ev.topic in ("avatar.walk_confirmed", "avatar.position"):
                x, y = ev.data.get("x"), ev.data.get("y")
                if x is not None and y is not None:
                    self.last_position = f"({x}, {y}, {ev.data.get('z', '?')})"
            if ev.topic == "avatar.health":
                hp, mx = ev.data.get("hits"), ev.data.get("hits_max")
                if hp is not None and mx is not None:
                    self.last_health = f"{hp}/{mx}"
            if ev.topic == "action.end":
                self.skill_results.append(ev)


def _ts(ts_str: str) -> str:
    try:
        return datetime.fromisoformat(ts_str).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return "??:??:??"


def _panel_activity(state: MonitorState, topic_filter: str, max_lines: int = 30) -> Panel:
    events = list(state.events)
    if topic_filter != "*":
        events = [e for e in events if fnmatch.fnmatch(e.topic, topic_filter)]
    events = events[-max_lines:]
    t = Text()
    for ev in events:
        prefix = ev.topic.split(".")[0] if ev.topic else "?"
        t.append(f" {_ts(ev.ts)} ", style="grey50")
        t.append(f"{ICONS.get(prefix, chr(0x2022))} ")
        t.append(f"[{ev.topic}] ", style="cyan")
        msg = ev.message[:80] if ev.message else ""
        t.append(f"{msg}\n", style="bold" if ev.importance >= 3 else "")
    if not events:
        t.append(" Waiting for events...", style="grey50")
    label = topic_filter if topic_filter != "*" else "all"
    return Panel(t, title=f"Activity [{label}]", border_style="bright_green")


def _panel_skills(state: MonitorState) -> Panel:
    t = Text()
    for ev in list(state.skill_results)[-12:]:
        name = ev.data.get("skill", "?")
        reward = ev.data.get("reward", 0.0)
        ok = ev.data.get("success", False)
        t.append(f" {_ts(ev.ts)} ", style="grey50")
        t.append("\u2714 " if ok else "\u2718 ", style="bright_green" if ok else "red")
        t.append(f"{name:<16} ", style="bright_white")
        rc = "bright_green" if reward > 0 else "red" if reward < 0 else "grey70"
        t.append(f"r={reward:+.1f}\n", style=rc)
    if not state.skill_results:
        t.append(" No skill results yet...", style="grey50")
    return Panel(t, title="Skill Results", border_style="bright_cyan")


def _panel_stats(state: MonitorState, paused: bool) -> Panel:
    t = Text()
    t.append("Position  ", style="bold")
    t.append(f"{state.last_position}\n")
    t.append("Health    ", style="bold")
    t.append(f"{state.last_health}\n\n")
    t.append("Event Counts:\n", style="bold")
    for prefix, count in sorted(state.event_counts.items(), key=lambda x: -x[1]):
        t.append(f"  {prefix:<12} ", style="bright_white")
        t.append(f"{count}\n", style="grey70")
    if paused:
        t.append("\n")
        t.append(" PAUSED ", style="bold white on red")
    return Panel(t, title="Stats", border_style="bright_blue")


class _KeyReader:
    def __init__(self) -> None:
        self._keys: list[str] = []
        self._lock = threading.Lock()
        self._stop = False

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop = True

    def poll(self) -> list[str]:
        with self._lock:
            keys = list(self._keys)
            self._keys.clear()
        return keys

    def _run(self) -> None:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not self._stop:
                try:
                    ch = os.read(fd, 1)
                    if ch:
                        with self._lock:
                            self._keys.append(ch.decode("ascii", errors="ignore"))
                except OSError:
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main() -> None:
    ap = argparse.ArgumentParser(description="Anima TUI Monitor")
    ap.add_argument("--events-file", default="data/events.jsonl")
    ap.add_argument("--refresh", type=float, default=0.5)
    ap.add_argument("--tail", type=int, default=50)
    args = ap.parse_args()

    events_path = Path(args.events_file)
    if not events_path.exists():
        print(f"Events file not found: {events_path}\nStart the agent first.")
        sys.exit(1)

    tailer = FileTailer(events_path)
    tailer.seek_tail(args.tail)
    state = MonitorState()
    state.ingest(tailer.read_new())
    paused = False
    filter_idx = 0

    def build() -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="main"), Layout(name="bottom", size=16), Layout(name="footer", size=1),
        )
        layout["main"].update(_panel_activity(state, FILTERS[filter_idx]))
        layout["bottom"].split_row(Layout(name="stats", ratio=1), Layout(name="skills", ratio=2))
        layout["bottom"]["stats"].update(_panel_stats(state, paused))
        layout["bottom"]["skills"].update(_panel_skills(state))
        ft = Text()
        for key, label in [("q", "Quit"), ("f", "Filter"), ("p", "Pause"), ("c", "Clear")]:
            ft.append(f" {key}", style="bold bright_yellow")
            ft.append(f" {label} ", style="grey70")
        layout["footer"].update(ft)
        return layout

    keys = _KeyReader()
    keys.start()
    try:
        with Live(build(), console=Console(), refresh_per_second=2, screen=True) as live:
            while True:
                for key in keys.poll():
                    if key == "q":
                        return
                    elif key == "f":
                        filter_idx = (filter_idx + 1) % len(FILTERS)
                    elif key == "p":
                        paused = not paused
                    elif key == "c":
                        state = MonitorState()
                if not paused:
                    new = tailer.read_new()
                    if new:
                        state.ingest(new)
                live.update(build())
                time.sleep(args.refresh)
    except KeyboardInterrupt:
        pass
    finally:
        keys.stop()


if __name__ == "__main__":
    main()
