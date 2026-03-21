#!/usr/bin/env python3
"""Standalone TUI monitor — reads data/state.json, renders Rich dashboard.

Runs as a separate process from the agent. No shared memory needed.

Usage:
    uv run python tools/tui.py
    uv run python tools/tui.py --refresh 1.0
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

STATE_FILE = Path("data/state.json")

# ---------------------------------------------------------------------------
# Key reader (non-blocking stdin)
# ---------------------------------------------------------------------------

_NOTORIETY_COLORS = {
    1: "dodger_blue1", 2: "green", 3: "grey70", 4: "grey70",
    5: "orange1", 6: "red", 7: "bright_yellow",
}
_LOCK_ICONS = {0: ("↑", "green"), 1: ("↓", "red"), 2: ("•", "grey50")}
_SKILL_NAMES = {
    0: "Alchemy", 1: "Anatomy", 2: "AnimalLore", 3: "ItemID",
    4: "ArmsLore", 5: "Parrying", 7: "Blacksmith", 8: "Bowcraft",
    9: "Peacemaking", 11: "Carpentry", 13: "Cooking", 17: "Healing",
    18: "Fishing", 21: "Hiding", 22: "Provocation", 23: "Inscription",
    25: "Magery", 26: "ResistSpells", 27: "Tactics", 29: "Musicianship",
    31: "Archery", 34: "Tailoring", 35: "Taming", 37: "Tinkering",
    38: "Tracking", 39: "Veterinary", 40: "Swords", 41: "Macing",
    42: "Fencing", 43: "Wrestling", 44: "Lumberjack", 45: "Mining",
    46: "Meditation", 47: "Stealth", 48: "RemoveTrap",
}
_CATEGORY_ICONS = {
    "brain": "⭐", "skill": "⚒", "combat": "⚔",
    "movement": "→", "social": "💬", "system": "ℹ",
    "action": "▶", "trade": "💰",
}


class _KeyReader:
    """Daemon thread that reads stdin one char at a time."""

    def __init__(self) -> None:
        self._keys: list[str] = []
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch:
                    self._keys.append(ch)
        except Exception:
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def drain(self) -> list[str]:
        keys = self._keys[:]
        self._keys.clear()
        return keys


# ---------------------------------------------------------------------------
# Panel renderers
# ---------------------------------------------------------------------------

def _panel_status(status: dict) -> Panel:
    t = Text()
    name = status.get("name", "?")
    t.append(f" {name}\n", style="bold bright_white")

    hp = status.get("hp", 0)
    hp_max = status.get("hp_max", 0)
    mana = status.get("mana", 0)
    mana_max = status.get("mana_max", 0)
    stam = status.get("stam", 0)
    stam_max = status.get("stam_max", 0)

    def bar(cur: int, mx: int, color: str) -> str:
        pct = cur / mx if mx else 0
        filled = int(pct * 10)
        return f"[{color}]{'█' * filled}{'░' * (10 - filled)}[/] {cur}/{mx}"

    t.append(" HP   ")
    t.append_text(Text.from_markup(bar(hp, hp_max, "red")))
    t.append("\n")
    t.append(" Mana ")
    t.append_text(Text.from_markup(bar(mana, mana_max, "blue")))
    t.append("\n")
    t.append(" Stam ")
    t.append_text(Text.from_markup(bar(stam, stam_max, "yellow")))
    t.append("\n")

    x = status.get("x", 0)
    y = status.get("y", 0)
    gold = status.get("gold", 0)
    wt = status.get("weight", 0)
    wt_max = status.get("weight_max", 0)
    t.append(f" Pos: ({x},{y})  Gold: {gold}\n", style="grey70")
    t.append(f" Weight: {wt}/{wt_max}", style="grey70")
    wt_pct = wt / wt_max * 100 if wt_max else 0
    if wt_pct > 80:
        t.append(f" ({wt_pct:.0f}%!)", style="bold red")
    t.append("\n")

    goal = status.get("goal", "")
    if goal:
        t.append(f" Goal: {goal}\n", style="bright_cyan")

    return Panel(t, title="Status", border_style="bright_blue")


def _panel_activity(activity: list[dict]) -> Panel:
    t = Text()
    for entry in activity[-15:]:
        cat = entry.get("topic", "").split(".")[0]
        icon = _CATEGORY_ICONS.get(cat, "·")
        msg = entry.get("message", "")[:70]
        imp = entry.get("importance", 1)
        style = "bold bright_white" if imp >= 3 else "" if imp >= 2 else "grey70"
        t.append(f" {icon} {msg}\n", style=style)
    if not activity:
        t.append(" Waiting for events...\n", style="grey50")
    return Panel(t, title="Activity", border_style="green")


def _panel_nearby(nearby: list[dict]) -> Panel:
    t = Text()
    for m in nearby[:8]:
        name = m.get("name", "?")[:16]
        dx = m.get("dx", 0)
        dy = m.get("dy", 0)
        nv = m.get("notoriety", 1)
        color = _NOTORIETY_COLORS.get(nv, "white")
        t.append(f" {name:<16}", style=color)
        t.append(f" ({dx:+d},{dy:+d})\n", style="grey70")
    if not nearby:
        t.append(" No one nearby\n", style="grey50")
    return Panel(t, title="Nearby", border_style="yellow")


def _panel_journal(journal: list[dict]) -> Panel:
    t = Text()
    for e in journal[-8:]:
        name = e.get("name", "?")[:12]
        text = e.get("text", "")
        style = "bright_cyan" if e.get("is_self") else ""
        t.append(f" {name}: ", style="bold " + style)
        t.append(f"{text}\n", style=style)
    if not journal:
        t.append(" No messages\n", style="grey50")
    return Panel(t, title="Journal", border_style="cyan")


def _panel_inventory(items: list[dict]) -> Panel:
    t = Text()
    for it in items[:12]:
        name = it.get("name", "?")
        amt = it.get("amount", 1)
        t.append(f" {name:<20}", style="bright_white")
        if amt > 1:
            t.append(f" x{amt}", style="bright_yellow")
        t.append("\n")
    if not items:
        t.append(" Empty\n", style="grey50")
    return Panel(t, title="Inventory", border_style="magenta")


def _panel_skills(skills: dict) -> Panel:
    t = Text()
    total = skills.get("total", 0)
    t.append(f" Total: {total:.1f}/700\n", style="grey70")
    for sk in skills.get("list", [])[:10]:
        sid = sk.get("id", 0)
        name = _SKILL_NAMES.get(sid, f"#{sid}")[:14]
        val = sk.get("value", 0)
        lock = sk.get("lock", 0)
        icon, color = _LOCK_ICONS.get(lock, ("?", "white"))
        t.append(f" {icon}", style=color)
        t.append(f" {name:<14} {val:5.1f}\n")
    return Panel(t, title="Skills", border_style="bright_green")


_MAP_COLORS = {
    "@": "bold bright_white",
    "X": "bold bright_red",
    "M": "bold bright_yellow",
    "#": "grey50",
    "T": "green",
    "+": "bright_cyan",
    ".": "grey23",
}


def _panel_minimap(minimap: dict) -> Panel:
    t = Text()
    rows = minimap.get("rows", [])
    px = minimap.get("px", 0)
    py = minimap.get("py", 0)

    if not rows:
        t.append(" No map data\n", style="grey50")
        return Panel(t, title=f"Map ({px},{py})", border_style="bright_blue")

    for row in rows:
        for ch in row:
            style = _MAP_COLORS.get(ch, "grey23")
            t.append(ch, style=style)
        t.append("\n")

    # Legend
    t.append(" @=you #=wall T=tree M=npc +=door X=goal\n", style="grey50")

    return Panel(t, title=f"Map ({px},{py})", border_style="bright_blue")


def _panel_qvalues(qv: dict) -> Panel:
    t = Text()
    for name, info in qv.items():
        q = info.get("q", 0)
        v = info.get("visits", 0)
        t.append(f" {name:<18}", style="bright_white")
        t.append(f" Q={q:<6.1f}", style="bright_cyan")
        t.append(f" ({v})\n", style="grey70")
    if not qv:
        t.append(" No Q-values yet\n", style="grey50")
    return Panel(t, title="Q-Values", border_style="bright_magenta")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def read_state() -> dict | None:
    """Read the state snapshot from file."""
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
        # Check freshness — stale if older than 5 seconds
        ts = data.get("ts", 0)
        if time.time() - ts > 5.0:
            data["_stale"] = True
        return data
    except (json.JSONDecodeError, OSError):
        return None


def build_layout(
    data: dict,
    show_inventory: bool = False,
    show_skills: bool = False,
    show_map: bool = False,
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="upper"),
        Layout(name="lower", size=14),
        Layout(name="footer", size=1),
    )

    upper_panels = [
        Layout(name="status", ratio=2, minimum_size=30),
        Layout(name="activity", ratio=3, minimum_size=40),
    ]
    if show_map:
        upper_panels.append(Layout(name="minimap", ratio=2, minimum_size=33))
    layout["upper"].split_row(*upper_panels)

    lower_panels = [Layout(name="nearby", ratio=1)]
    lower_panels.append(Layout(name="journal", ratio=1))
    if show_inventory:
        lower_panels.append(Layout(name="inventory", ratio=1))
    if show_skills:
        lower_panels.append(Layout(name="skills", ratio=1))
    lower_panels.append(Layout(name="qvalues", ratio=1))
    layout["lower"].split_row(*lower_panels)

    status = data.get("status", {})
    layout["status"].update(_panel_status(status))
    layout["activity"].update(_panel_activity(data.get("activity", [])))
    if show_map:
        layout["minimap"].update(_panel_minimap(data.get("minimap", {})))
    layout["nearby"].update(_panel_nearby(data.get("nearby", [])))
    layout["journal"].update(_panel_journal(data.get("journal", [])))
    if show_inventory:
        layout["inventory"].update(_panel_inventory(data.get("inventory", [])))
    if show_skills:
        layout["skills"].update(_panel_skills(data.get("skills", {})))
    layout["qvalues"].update(_panel_qvalues(data.get("qvalues", {})))

    # Footer
    footer = Text()
    stale = data.get("_stale", False)
    if stale:
        footer.append(" ⚠ STALE ", style="bold red")
    else:
        footer.append(" ● LIVE ", style="bold green")
    footer.append("  ")
    footer.append("m", style="bold bright_yellow")
    footer.append(" Map  ", style="grey70")
    footer.append("i", style="bold bright_yellow")
    footer.append(" Inventory  ", style="grey70")
    footer.append("s", style="bold bright_yellow")
    footer.append(" Skills  ", style="grey70")
    footer.append("q", style="bold bright_yellow")
    footer.append(" Quit", style="grey70")
    layout["footer"].update(footer)

    return layout


def main() -> None:
    import termios

    parser = argparse.ArgumentParser(description="Anima TUI Monitor (standalone)")
    parser.add_argument("--refresh", type=float, default=0.5, help="Refresh rate")
    args = parser.parse_args()

    # Save terminal settings before anything touches them
    fd = sys.stdin.fileno()
    old_term = termios.tcgetattr(fd)

    console = Console()
    key_reader = _KeyReader()
    key_reader.start()

    show_inventory = False
    show_skills = False
    show_map = False

    # Initial empty layout
    empty = {"status": {}, "activity": [], "nearby": [], "journal": []}

    try:
        with Live(
            build_layout(empty),
            console=console,
            refresh_per_second=int(1 / args.refresh),
            screen=True,
        ) as live:
            try:
                while True:
                    data = read_state() or empty

                    # Handle keys
                    for key in key_reader.drain():
                        if key == "q":
                            return
                        elif key == "m":
                            show_map = not show_map
                        elif key == "i":
                            show_inventory = not show_inventory
                        elif key == "s":
                            show_skills = not show_skills

                    live.update(build_layout(
                        data,
                        show_inventory=show_inventory,
                        show_skills=show_skills,
                        show_map=show_map,
                    ))
                    time.sleep(args.refresh)
            except KeyboardInterrupt:
                pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_term)


if __name__ == "__main__":
    main()
