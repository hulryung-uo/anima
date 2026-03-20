"""Rich Live TUI — real-time terminal dashboard for Anima.

Uses Rich Live for rendering (proven reliable) and a dedicated
stdin reader thread for key input (non-blocking, no asyncio conflict).
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from datetime import datetime
from typing import TYPE_CHECKING

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    from anima.monitor.feed import ActivityFeed
    from anima.perception import Perception

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
# Panel builders — each returns a Rich Panel
# ---------------------------------------------------------------------------

def _panel_status(p: "Perception", bb: dict) -> Panel:
    ss = p.self_state
    persona = bb.get("persona")
    name = persona.name if persona else "Anima"
    title = getattr(persona, "title", "") if persona else ""
    goal = bb.get("current_goal")
    goal_text = goal.get("description", "")[:50] if goal else "none"

    t = Text()
    t.append(name, style="bold bright_white")
    t.append(f" — {title}\n\n")
    for label, style, cur, mx, sl, sv in [
        ("HP  ", "bold red", ss.hits, ss.hits_max, "STR", ss.strength),
        ("Mana", "bold blue", ss.mana, ss.mana_max, "DEX", ss.dexterity),
        ("Stam", "bold yellow", ss.stam, ss.stam_max, "INT", ss.intelligence),
    ]:
        t.append(f"{label} ", style=style)
        t.append_text(_bar(cur, mx))
        t.append(f"  {sl} ", style="bold")
        t.append(f"{sv}\n")
    t.append(f"\nPos ({ss.x}, {ss.y}, {ss.z})  ", style="grey70")
    t.append(f"Gold {ss.gold:,}  ", style="bright_yellow")
    t.append(f"Wt {ss.weight}/{ss.weight_max}\n", style="grey70")
    t.append("Goal ", style="bright_green")
    t.append(goal_text)
    return Panel(t, title="Status", border_style="bright_blue")


def _panel_activity(feed: "ActivityFeed") -> Panel:
    events = feed.recent(18)
    t = Text()
    for ev in events:
        ts = datetime.fromtimestamp(ev.timestamp).strftime("%H:%M:%S")
        icon = _CATEGORY_ICONS.get(ev.category, "\u2022")
        t.append(f" {ts} ", style="grey50")
        t.append(f"{icon} ")
        t.append(f"{ev.message}\n", style="bold" if ev.importance >= 3 else "")
    if not events:
        t.append(" Waiting for activity...", style="grey50")
    return Panel(t, title="Activity", border_style="bright_green")


def _panel_nearby(p: "Perception") -> Panel:
    ss = p.self_state
    mobs = p.world.nearby_mobiles(ss.x, ss.y, distance=18)
    mobs.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))
    t = Text()
    for mob in mobs[:8]:
        name = (mob.name or f"0x{mob.body:04X}")[:18]
        dx, dy = mob.x - ss.x, mob.y - ss.y
        dirs = []
        if dy < 0:
            dirs.append(f"{abs(dy)}N")
        elif dy > 0:
            dirs.append(f"{abs(dy)}S")
        if dx > 0:
            dirs.append(f"{abs(dx)}E")
        elif dx < 0:
            dirs.append(f"{abs(dx)}W")
        nv = mob.notoriety.value if mob.notoriety else 1
        t.append(name, style=_NOTORIETY_COLORS.get(nv, "white"))
        t.append(f"  {','.join(dirs) or 'here'}\n", style="grey70")
    if not mobs:
        t.append("nobody nearby", style="grey50")
    return Panel(t, title="Nearby", border_style="bright_yellow")


def _panel_journal(p: "Perception") -> Panel:
    entries = p.social.recent(count=10)
    my = p.self_state.serial
    t = Text()
    for e in entries:
        ts = datetime.fromtimestamp(e.timestamp).strftime("%H:%M:%S")
        name = e.name or "?"
        s = ("bright_cyan" if e.serial == my
             else "grey50" if name.lower() == "system"
             else "bright_white")
        t.append(f"{ts} ", style="grey50")
        t.append(f"{name}: ", style=s)
        t.append(f"{e.text[:55]}\n")
    if not entries:
        t.append("No speech yet...", style="grey50")
    return Panel(t, title="Journal", border_style="bright_magenta")


def _panel_inventory(p: "Perception") -> Panel:
    bp = p.self_state.equipment.get(0x15)
    t = Text()
    if bp:
        items = sorted(
            [it for it in p.world.items.values() if it.container == bp],
            key=lambda it: it.name or "",
        )
        for it in items[:12]:
            name = it.name or f"0x{it.graphic:04X}"
            t.append(f"{name[:20]}")
            if it.amount > 1:
                t.append(f" x{it.amount}", style="grey70")
            t.append("\n")
        if not items:
            t.append("empty", style="grey50")
    else:
        t.append("no backpack", style="grey50")
    return Panel(t, title="Inventory", border_style="bright_white")


def _panel_skills(p: "Perception") -> Panel:
    skills = sorted(p.self_state.skills.values(), key=lambda s: (-s.value, s.id))
    t = Text()
    total = 0.0
    n = 0
    for sk in skills:
        if sk.value == 0 and sk.lock.value == 2:
            continue
        total += sk.value
        name = _SKILL_NAMES.get(sk.id, f"Skill{sk.id}")
        icon, color = _LOCK_ICONS.get(sk.lock.value, ("?", "white"))
        t.append(f"{icon} ", style=color)
        t.append(f"{name[:12]:<12} ")
        t.append(f"{sk.value:5.1f}", style="bright_white")
        t.append(f"/{sk.cap:.0f}\n", style="grey50")
        n += 1
        if n >= 12:
            break
    t.append(f"\nTotal {total:.1f}/700", style="bold")
    return Panel(t, title="Skills", border_style="bright_yellow")


def _panel_qvalues(bb: dict) -> Panel:
    qs: dict[str, tuple[float, int]] = bb.get("q_snapshot", {})
    t = Text()
    if qs:
        for name, (q, v) in sorted(
            qs.items(), key=lambda x: x[1][0], reverse=True
        )[:8]:
            c = "bright_green" if q > 0 else "red" if q < 0 else "grey70"
            t.append(f"{name[:16]:<16} ")
            t.append(f"Q={q:.2f} ", style=c)
            t.append(f"n={v}\n", style="grey70")
    else:
        t.append("no data yet", style="grey50")
    return Panel(t, title="Q-Values", border_style="bright_cyan")


# ---------------------------------------------------------------------------
# Key reader thread — reads single chars from stdin in cbreak mode
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
# TUI main class
# ---------------------------------------------------------------------------

class AnimaTUI:
    """Rich Live TUI with threaded key input."""

    def __init__(
        self,
        perception: "Perception",
        feed: "ActivityFeed",
        blackboard: dict,
        refresh_rate: float = 0.5,
    ) -> None:
        self._p = perception
        self._feed = feed
        self._bb = blackboard
        self._refresh = refresh_rate
        self._show_inventory = False
        self._show_skills = False

    def _build(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="upper"),
            Layout(name="lower", size=14),
            Layout(name="footer", size=1),
        )
        layout["upper"].split_row(
            Layout(name="status", ratio=2, minimum_size=30),
            Layout(name="activity", ratio=3, minimum_size=40),
        )

        # Build lower panels based on toggle state
        lower_panels = [Layout(name="nearby", ratio=1)]
        lower_panels.append(Layout(name="journal", ratio=1))
        if self._show_inventory:
            lower_panels.append(Layout(name="inventory", ratio=1))
        if self._show_skills:
            lower_panels.append(Layout(name="skills", ratio=1))
        lower_panels.append(Layout(name="qvalues", ratio=1))
        layout["lower"].split_row(*lower_panels)

        # Render panels
        layout["status"].update(_panel_status(self._p, self._bb))
        layout["activity"].update(_panel_activity(self._feed))
        layout["nearby"].update(_panel_nearby(self._p))
        layout["journal"].update(_panel_journal(self._p))
        if self._show_inventory:
            layout["inventory"].update(_panel_inventory(self._p))
        if self._show_skills:
            layout["skills"].update(_panel_skills(self._p))
        layout["qvalues"].update(_panel_qvalues(self._bb))

        # Footer
        footer = Text()
        footer.append(" j", style="bold bright_yellow")
        footer.append(" Journal  ", style="grey70")
        footer.append("i", style="bold bright_yellow")
        footer.append(" Inventory  ", style="grey70")
        footer.append("s", style="bold bright_yellow")
        footer.append(" Skills  ", style="grey70")
        footer.append("q", style="bold bright_yellow")
        footer.append(" Quit", style="grey70")
        layout["footer"].update(footer)

        return layout

    def _handle_keys(self, keys: list[str]) -> bool:
        """Handle key presses. Returns True if quit requested."""
        for key in keys:
            if key == "j":
                # Journal is always shown — no toggle needed
                pass
            elif key == "i":
                self._show_inventory = not self._show_inventory
            elif key == "s":
                self._show_skills = not self._show_skills
            elif key == "q":
                return True
        return False

    async def run(self) -> None:
        console = Console()
        key_reader = _KeyReader()
        key_reader.start()

        try:
            with Live(
                self._build(),
                console=console,
                refresh_per_second=2,
                screen=True,
            ) as live:
                while True:
                    # Handle key input
                    keys = key_reader.poll()
                    if self._handle_keys(keys):
                        break

                    # Update display
                    live.update(self._build())
                    await asyncio.sleep(self._refresh)
        finally:
            key_reader.stop()

        # q pressed — signal shutdown, then exit
        self._bb["shutdown_requested"] = True
        # Give brain_loop a moment to write final forum post
        await asyncio.sleep(2.0)

        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()
        os._exit(0)
