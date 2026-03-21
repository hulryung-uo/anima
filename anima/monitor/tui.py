"""AnimaMonitor — EventBus subscriber TUI dashboard.

Subscribes to ``monitor.*`` topics published by StatePublisher
and renders a Rich Live terminal dashboard. Does not access
Perception or blackboard directly.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    from anima.core.bus import EventBus, Subscription
    from anima.map import MapReader

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------

_NOTORIETY_COLORS = {
    1: "dodger_blue1", 2: "green", 3: "grey70", 4: "grey70",
    5: "orange1", 6: "red", 7: "bright_yellow",
}
_CATEGORY_ICONS = {
    "brain": "\u2b50", "skill": "\u2692", "combat": "\u2694",
    "movement": "\u2192", "social": "\U0001f4ac", "system": "\u2139",
    "action": "\u2692", "avatar": "\u2022",
}
_LOCK_ICONS = {0: ("\u2191", "green"), 1: ("\u2193", "red"), 2: ("\u2022", "grey50")}
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


def _bar(cur: int, mx: int, width: int = 10) -> Text:
    ratio = cur / mx if mx else 1.0
    filled = int(ratio * width)
    color = "red" if ratio < 0.25 else "yellow" if ratio < 0.5 else "green"
    t = Text()
    t.append("\u2588" * filled, style=color)
    t.append("\u2591" * (width - filled), style="grey30")
    t.append(f" {cur}/{mx}")
    return t


# ---------------------------------------------------------------------------
# Panel builders — each takes a plain dict (from EventBus data)
# ---------------------------------------------------------------------------

def _panel_status(status: dict[str, Any]) -> Panel:
    name = status.get("name", "Anima")
    title = status.get("title", "")
    goal = status.get("goal", "none")

    t = Text()
    t.append(name, style="bold bright_white")
    t.append(f" \u2014 {title}\n\n")
    for label, style, ck, mk, sl, sk in [
        ("HP  ", "bold red", "hp", "hp_max", "STR", "str"),
        ("Mana", "bold blue", "mana", "mana_max", "DEX", "dex"),
        ("Stam", "bold yellow", "stam", "stam_max", "INT", "int"),
    ]:
        cur = status.get(ck, 0)
        mx = status.get(mk, 0)
        sv = status.get(sk, 0)
        t.append(f"{label} ", style=style)
        t.append_text(_bar(cur, mx))
        t.append(f"  {sl} ", style="bold")
        t.append(f"{sv}\n")

    x = status.get("x", 0)
    y = status.get("y", 0)
    z = status.get("z", 0)
    gold = status.get("gold", 0)
    weight = status.get("weight", 0)
    weight_max = status.get("weight_max", 0)

    t.append(f"\nPos ({x}, {y}, {z})  ", style="grey70")
    t.append(f"Gold {gold:,}  ", style="bright_yellow")
    t.append(f"Wt {weight}/{weight_max}\n", style="grey70")
    t.append("Goal ", style="bright_green")
    t.append(goal)
    return Panel(t, title="Status", border_style="bright_blue")


def _panel_activity(events: list[dict[str, Any]]) -> Panel:
    t = Text()
    for ev in events[-18:]:
        ts = datetime.fromtimestamp(ev["timestamp"]).strftime("%H:%M:%S")
        icon = _CATEGORY_ICONS.get(ev.get("category", ""), "\u2022")
        imp = ev.get("importance", 1)
        t.append(f" {ts} ", style="grey50")
        t.append(f"{icon} ")
        t.append(f"{ev.get('message', '')}\n", style="bold" if imp >= 3 else "")
    if not events:
        t.append(" Waiting for activity...", style="grey50")
    return Panel(t, title="Activity", border_style="bright_green")


def _panel_nearby(nearby: dict[str, Any]) -> Panel:
    mobs = nearby.get("mobiles", [])
    t = Text()
    for mob in mobs[:8]:
        name = mob.get("name", "?")[:18]
        dx, dy = mob.get("dx", 0), mob.get("dy", 0)
        dirs: list[str] = []
        if dy < 0:
            dirs.append(f"{abs(dy)}N")
        elif dy > 0:
            dirs.append(f"{abs(dy)}S")
        if dx > 0:
            dirs.append(f"{abs(dx)}E")
        elif dx < 0:
            dirs.append(f"{abs(dx)}W")
        nv = mob.get("notoriety", 1)
        t.append(name, style=_NOTORIETY_COLORS.get(nv, "white"))
        t.append(f"  {','.join(dirs) or 'here'}\n", style="grey70")
    if not mobs:
        t.append("nobody nearby", style="grey50")
    return Panel(t, title="Nearby", border_style="bright_yellow")


def _panel_journal(journal: dict[str, Any]) -> Panel:
    entries = journal.get("entries", [])
    t = Text()
    for e in entries:
        ts = datetime.fromtimestamp(e["timestamp"]).strftime("%H:%M:%S")
        name = e.get("name", "?")
        is_self = e.get("is_self", False)
        s = ("bright_cyan" if is_self
             else "grey50" if name.lower() == "system"
             else "bright_white")
        t.append(f"{ts} ", style="grey50")
        t.append(f"{name}: ", style=s)
        t.append(f"{e.get('text', '')}\n")
    if not entries:
        t.append("No speech yet...", style="grey50")
    return Panel(t, title="Journal", border_style="bright_magenta")


def _panel_inventory(inventory: dict[str, Any]) -> Panel:
    items = inventory.get("items", [])
    has_backpack = inventory.get("has_backpack", False)
    t = Text()
    if has_backpack:
        for it in items[:12]:
            t.append(it.get("name", "?"))
            amt = it.get("amount", 1)
            if amt > 1:
                t.append(f" x{amt}", style="grey70")
            t.append("\n")
        if not items:
            t.append("empty", style="grey50")
    else:
        t.append("no backpack", style="grey50")
    return Panel(t, title="Inventory", border_style="bright_white")


def _panel_skills(skills_data: dict[str, Any]) -> Panel:
    skills = skills_data.get("skills", [])
    total = skills_data.get("total", 0.0)
    t = Text()
    for sk in skills[:12]:
        name = _SKILL_NAMES.get(sk["id"], f"Skill{sk['id']}")
        icon, color = _LOCK_ICONS.get(sk.get("lock", 2), ("?", "white"))
        t.append(f"{icon} ", style=color)
        t.append(f"{name[:12]:<12} ")
        t.append(f"{sk['value']:5.1f}", style="bright_white")
        t.append(f"/{sk['cap']:.0f}\n", style="grey50")
    t.append(f"\nTotal {total:.1f}/700", style="bold")
    return Panel(t, title="Skills", border_style="bright_yellow")


def _panel_minimap(
    status: dict[str, Any],
    nearby: dict[str, Any],
    map_reader: "MapReader | None",
) -> Panel:
    sx = status.get("x", 0)
    sy = status.get("y", 0)
    sz = status.get("z", 0)
    move_target = status.get("move_target")

    radius = 12
    t = Text()

    if map_reader is None:
        t.append("No map reader", style="grey50")
        return Panel(t, title="Map", border_style="bright_blue")

    mobs = {(m["x"], m["y"]): m for m in nearby.get("mobiles", [])}
    goal = tuple(move_target[:2]) if move_target else None

    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            x, y = sx + dx, sy + dy

            if dx == 0 and dy == 0:
                t.append("@", style="bold bright_green")
            elif goal and x == goal[0] and y == goal[1]:
                t.append("X", style="bold bright_red")
            elif (x, y) in mobs:
                mob = mobs[(x, y)]
                nv = mob.get("notoriety", 1)
                color = _NOTORIETY_COLORS.get(nv, "white")
                t.append("M", style=color)
            else:
                tile = map_reader.get_tile(x, y)
                can, _ = tile.walkable_z(sz)
                if not can:
                    has_wall = any(
                        s.impassable and not s.surface for s in tile.statics
                    )
                    if has_wall:
                        t.append("#", style="grey30")
                    else:
                        t.append("~", style="blue")
                else:
                    has_tree = any(
                        s.graphic in range(0x0CCA, 0x0D9C) for s in tile.statics
                    )
                    if has_tree:
                        t.append("T", style="green")
                    else:
                        t.append(".", style="grey23")
        t.append("\n")

    return Panel(t, title="Map", border_style="bright_blue")


def _panel_qvalues(qv_data: dict[str, Any]) -> Panel:
    values = qv_data.get("values", {})
    t = Text()
    if values:
        for name, info in values.items():
            q = info["q"]
            v = info["visits"]
            c = "bright_green" if q > 0 else "red" if q < 0 else "grey70"
            t.append(f"{name[:16]:<16} ")
            t.append(f"Q={q:.2f} ", style=c)
            t.append(f"n={v}\n", style="grey70")
    else:
        t.append("no data yet", style="grey50")
    return Panel(t, title="Q-Values", border_style="bright_cyan")


# ---------------------------------------------------------------------------
# Key reader thread
# ---------------------------------------------------------------------------

class _KeyReader:
    """Reads keys from stdin in a background thread."""

    def __init__(self) -> None:
        self._keys: list[str] = []
        self._lock = threading.Lock()
        self._stop = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

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


# ---------------------------------------------------------------------------
# AnimaMonitor — EventBus subscriber TUI
# ---------------------------------------------------------------------------

class AnimaMonitor:
    """Rich Live TUI that subscribes to EventBus for all state."""

    def __init__(
        self,
        bus: "EventBus",
        map_reader: "MapReader | None" = None,
        refresh_rate: float = 0.5,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        self._bus = bus
        self._map_reader = map_reader
        self._refresh = refresh_rate
        self._shutdown_event = shutdown_event

        # Subscriptions (for cleanup)
        self._subs: list[Subscription] = []

        # Cached state from EventBus
        self._status: dict[str, Any] = {}
        self._nearby: dict[str, Any] = {"mobiles": []}
        self._journal: dict[str, Any] = {"entries": []}
        self._inventory: dict[str, Any] = {"items": [], "has_backpack": False}
        self._skills: dict[str, Any] = {"skills": [], "total": 0.0}
        self._qvalues: dict[str, Any] = {"values": {}}
        self._activity: deque[dict[str, Any]] = deque(maxlen=200)

        # UI toggles
        self._show_inventory = False
        self._show_skills = False
        self._show_map = True

    # -- Subscriber lifecycle -----------------------------------------------

    def connect(self) -> None:
        """Register with the EventBus."""
        topic_handlers = {
            "monitor.status": self._on_status,
            "monitor.nearby": self._on_nearby,
            "monitor.journal": self._on_journal,
            "monitor.inventory": self._on_inventory,
            "monitor.skills": self._on_skills,
            "monitor.qvalues": self._on_qvalues,
        }
        for pattern, handler in topic_handlers.items():
            self._subs.append(self._bus.subscribe(pattern, handler))

        # All events for the activity panel
        self._subs.append(self._bus.subscribe("*", self._on_activity))

    def disconnect(self) -> None:
        """Unregister from the EventBus."""
        for sub in self._subs:
            self._bus.unsubscribe(sub)
        self._subs.clear()

    # -- Event handlers -----------------------------------------------------

    def _on_status(self, _topic: str, data: dict[str, Any]) -> None:
        self._status = data

    def _on_nearby(self, _topic: str, data: dict[str, Any]) -> None:
        self._nearby = data

    def _on_journal(self, _topic: str, data: dict[str, Any]) -> None:
        self._journal = data

    def _on_inventory(self, _topic: str, data: dict[str, Any]) -> None:
        self._inventory = data

    def _on_skills(self, _topic: str, data: dict[str, Any]) -> None:
        self._skills = data

    def _on_qvalues(self, _topic: str, data: dict[str, Any]) -> None:
        self._qvalues = data

    def _on_activity(self, topic: str, data: dict[str, Any]) -> None:
        # Skip state-snapshot topics — they aren't user-facing activities
        if topic.startswith("monitor."):
            return
        message = data.get("message", "")
        if not message:
            return
        category = topic.split(".")[0]
        self._activity.append({
            "timestamp": time.time(),
            "category": category,
            "message": message,
            "importance": data.get("importance", 1),
        })

    # -- Layout / rendering -------------------------------------------------

    def _build(self) -> Layout:
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
        if self._show_map:
            upper_panels.append(Layout(name="minimap", ratio=2, minimum_size=26))
        layout["upper"].split_row(*upper_panels)

        lower_panels = [Layout(name="nearby", ratio=1)]
        lower_panels.append(Layout(name="journal", ratio=1))
        if self._show_inventory:
            lower_panels.append(Layout(name="inventory", ratio=1))
        if self._show_skills:
            lower_panels.append(Layout(name="skills", ratio=1))
        lower_panels.append(Layout(name="qvalues", ratio=1))
        layout["lower"].split_row(*lower_panels)

        # Render panels from cached state
        layout["status"].update(_panel_status(self._status))
        layout["activity"].update(_panel_activity(list(self._activity)))
        if self._show_map:
            layout["minimap"].update(
                _panel_minimap(self._status, self._nearby, self._map_reader)
            )
        layout["nearby"].update(_panel_nearby(self._nearby))
        layout["journal"].update(_panel_journal(self._journal))
        if self._show_inventory:
            layout["inventory"].update(_panel_inventory(self._inventory))
        if self._show_skills:
            layout["skills"].update(_panel_skills(self._skills))
        layout["qvalues"].update(_panel_qvalues(self._qvalues))

        # Footer
        subs = self._bus.subscriber_count
        footer = Text()
        footer.append(f" [{subs} subs] ", style="grey50")
        footer.append("i", style="bold bright_yellow")
        footer.append(" Inventory  ", style="grey70")
        footer.append("s", style="bold bright_yellow")
        footer.append(" Skills  ", style="grey70")
        footer.append("m", style="bold bright_yellow")
        footer.append(" Map  ", style="grey70")
        footer.append("q", style="bold bright_yellow")
        footer.append(" Quit", style="grey70")
        layout["footer"].update(footer)

        return layout

    def _handle_keys(self, keys: list[str]) -> bool:
        """Returns True if quit requested."""
        for key in keys:
            if key == "i":
                self._show_inventory = not self._show_inventory
            elif key == "s":
                self._show_skills = not self._show_skills
            elif key == "m":
                self._show_map = not self._show_map
            elif key == "q":
                return True
        return False

    # -- Main loop ----------------------------------------------------------

    async def run(self) -> None:
        """Subscribe, render, and handle input until quit."""
        import termios

        self.connect()
        console = Console()
        key_reader = _KeyReader()

        # Save terminal settings before anything touches them
        fd = sys.stdin.fileno()
        old_term = termios.tcgetattr(fd)

        key_reader.start()

        try:
            with Live(
                self._build(),
                console=console,
                refresh_per_second=2,
                screen=True,
            ) as live:
                while True:
                    keys = key_reader.poll()
                    if self._handle_keys(keys):
                        break
                    live.update(self._build())
                    await asyncio.sleep(self._refresh)
        finally:
            key_reader.stop()
            self.disconnect()
            termios.tcsetattr(fd, termios.TCSADRAIN, old_term)

        # Signal shutdown to the agent
        if self._shutdown_event:
            self._shutdown_event.set()
            await asyncio.sleep(2.0)

        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()
        os._exit(0)
