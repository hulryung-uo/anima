"""Textual TUI dashboard for Anima.

Architecture: Textual App owns the event loop. Game coroutines
(recv_loop, brain_loop, inspect_self) are launched as asyncio.Tasks
inside on_mount(), so everything runs in one event loop.

Panels:
  - Status:    HP/Mana/Stam bars, stats, position, goal
  - Activity:  ActivityFeed events (brain decisions, skill actions, movement)
  - Nearby:    Mobiles within 18 tiles
  - Journal:   Recent speech/system messages
  - Inventory: Backpack contents (toggle: i)
  - Skills:    Character skills with lock state (toggle: s)
  - Q-Values:  RL Q-table snapshot

Key bindings:
  j = toggle Journal | i = toggle Inventory | s = toggle Skills | q = quit
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, Coroutine

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Static

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


def _hp_bar(cur: int, mx: int, width: int = 10) -> Text:
    """Render a colored HP/mana/stam bar as a Rich Text object."""
    ratio = cur / mx if mx else 1.0
    filled = int(ratio * width)
    color = "red" if ratio < 0.25 else "yellow" if ratio < 0.5 else "green"
    t = Text()
    t.append("\u2588" * filled, style=color)
    t.append("\u2591" * (width - filled), style="grey30")
    t.append(f" {cur}/{mx}")
    return t


# ---------------------------------------------------------------------------
# Panel renderers — each returns a Rich Text object
# ---------------------------------------------------------------------------

def _render_status(p: "Perception", bb: dict) -> Text:
    ss = p.self_state
    persona = bb.get("persona")
    name = persona.name if persona else "Anima"
    title = getattr(persona, "title", "") if persona else ""
    goal = bb.get("current_goal")
    goal_text = goal.get("description", "")[:50] if goal else "none"

    t = Text()
    t.append(name, style="bold bright_white")
    t.append(f" — {title}\n\n")

    for label, style, cur, mx, stat_label, stat_val in [
        ("HP  ", "bold red",    ss.hits, ss.hits_max, "STR", ss.strength),
        ("Mana", "bold blue",   ss.mana, ss.mana_max, "DEX", ss.dexterity),
        ("Stam", "bold yellow", ss.stam, ss.stam_max, "INT", ss.intelligence),
    ]:
        t.append(f"{label} ", style=style)
        t.append_text(_hp_bar(cur, mx))
        t.append(f"  {stat_label} ", style="bold")
        t.append(f"{stat_val}\n")

    t.append(f"\nPos ({ss.x}, {ss.y}, {ss.z})  ", style="grey70")
    t.append(f"Gold {ss.gold:,}  ", style="bright_yellow")
    t.append(f"Wt {ss.weight}/{ss.weight_max}\n", style="grey70")
    t.append("Goal ", style="bright_green")
    t.append(goal_text)
    return t


def _render_activity(feed: "ActivityFeed") -> Text:
    events = feed.recent(16)
    t = Text()
    for ev in events:
        ts = datetime.fromtimestamp(ev.timestamp).strftime("%H:%M:%S")
        icon = _CATEGORY_ICONS.get(ev.category, "\u2022")
        t.append(f"{ts} ", style="grey50")
        t.append(f"{icon} ")
        t.append(f"{ev.message}\n", style="bold" if ev.importance >= 3 else "")
    if not events:
        t.append("Waiting for activity...", style="grey50")
    return t


def _render_nearby(p: "Perception") -> Text:
    ss = p.self_state
    mobs = p.world.nearby_mobiles(ss.x, ss.y, distance=18)
    mobs.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))
    t = Text()
    for mob in mobs[:10]:
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
    return t


def _render_journal(p: "Perception") -> Text:
    entries = p.social.recent(count=12)
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
    return t


def _render_inventory(p: "Perception") -> Text:
    bp = p.self_state.equipment.get(0x15)
    t = Text()
    if bp:
        items = sorted(
            [it for it in p.world.items.values() if it.container == bp],
            key=lambda it: it.name or "",
        )
        for it in items[:14]:
            name = it.name or f"0x{it.graphic:04X}"
            t.append(f"{name[:20]}")
            if it.amount > 1:
                t.append(f" x{it.amount}", style="grey70")
            t.append("\n")
        if not items:
            t.append("empty", style="grey50")
    else:
        t.append("no backpack", style="grey50")
    return t


def _render_skills(p: "Perception") -> Text:
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
        if n >= 14:
            break
    t.append(f"\nTotal {total:.1f}/700", style="bold")
    return t


def _render_qvalues(bb: dict) -> Text:
    qs: dict[str, tuple[float, int]] = bb.get("q_snapshot", {})
    t = Text()
    if qs:
        for name, (q, v) in sorted(qs.items(), key=lambda x: x[1][0], reverse=True)[:8]:
            c = "bright_green" if q > 0 else "red" if q < 0 else "grey70"
            t.append(f"{name[:16]:<16} ")
            t.append(f"Q={q:.2f} ", style=c)
            t.append(f"n={v}\n", style="grey70")
    else:
        t.append("no data yet", style="grey50")
    return t


# ---------------------------------------------------------------------------
# Textual App
# ---------------------------------------------------------------------------

class AnimaTUI(App):
    """Anima TUI dashboard. Owns the event loop."""

    TITLE = "Anima"
    CSS = """
    #top-row { height: 12; }
    #mid-row { height: 12; }
    #bot-row { height: 12; }
    .panel { border: round gray; padding: 0 1; }
    #p-status { width: 2fr; }
    #p-activity { width: 3fr; }
    """

    BINDINGS = [
        Binding("j", "toggle_journal", "Journal", show=True),
        Binding("i", "toggle_inventory", "Inventory", show=True),
        Binding("s", "toggle_skills", "Skills", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(
        self,
        perception: "Perception",
        feed: "ActivityFeed",
        blackboard: dict,
        refresh_rate: float = 0.5,
        background_tasks: list[Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        super().__init__()
        self._p = perception
        self._feed = feed
        self._bb = blackboard
        self._rate = refresh_rate
        self._bg_coros = background_tasks or []
        self._tasks: list[asyncio.Task] = []

    # -- Layout --

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-row"):
            yield Static("Loading...", id="p-status", classes="panel")
            yield Static("Loading...", id="p-activity", classes="panel")
        with Horizontal(id="mid-row"):
            yield Static("Nearby", id="p-nearby", classes="panel")
            yield Static("Journal", id="p-journal", classes="panel")
            yield Static("Q-Values", id="p-qvalues", classes="panel")
        with Horizontal(id="bot-row"):
            yield Static("Inventory", id="p-inventory", classes="panel")
            yield Static("Skills", id="p-skills", classes="panel")
        yield Footer()

    # -- Lifecycle --

    def on_mount(self) -> None:
        self._log_error(f"on_mount called, {len(self._bg_coros)} background tasks")
        # Launch game coroutines as background tasks in the same event loop
        for coro in self._bg_coros:
            self._tasks.append(asyncio.create_task(coro))
        # Periodic UI refresh
        self.set_interval(self._rate, self._tick)
        self._log_error("set_interval started")

    async def action_quit(self) -> None:
        for task in self._tasks:
            task.cancel()
        self.exit()

    # -- Key actions (use on_key for reliable capture without focus) --

    def on_key(self, event) -> None:
        self._log_error(f"on_key: {event.character!r} key={event.key!r}")
        key = event.character
        if key == "j":
            w = self.query_one("#p-journal")
            w.display = not w.display
            event.prevent_default()
        elif key == "i":
            w = self.query_one("#p-inventory")
            w.display = not w.display
            event.prevent_default()
        elif key == "s":
            w = self.query_one("#p-skills")
            w.display = not w.display
            event.prevent_default()

    def action_toggle_journal(self) -> None:
        w = self.query_one("#p-journal")
        w.display = not w.display

    def action_toggle_inventory(self) -> None:
        w = self.query_one("#p-inventory")
        w.display = not w.display

    def action_toggle_skills(self) -> None:
        w = self.query_one("#p-skills")
        w.display = not w.display

    # -- Periodic refresh --

    _tick_count: int = 0

    def _tick(self) -> None:
        """Update all visible panels."""
        self._tick_count += 1
        try:
            self.query_one("#p-status").update(_render_status(self._p, self._bb))
            self.query_one("#p-activity").update(_render_activity(self._feed))
            self.query_one("#p-nearby").update(_render_nearby(self._p))
            self.query_one("#p-journal").update(_render_journal(self._p))
            self.query_one("#p-inventory").update(_render_inventory(self._p))
            self.query_one("#p-skills").update(_render_skills(self._p))
            self.query_one("#p-qvalues").update(_render_qvalues(self._bb))
            if self._tick_count <= 3:
                self._log_error(f"tick #{self._tick_count}: "
                                f"hp={self._p.self_state.hits}/{self._p.self_state.hits_max}, "
                                f"skills={len(self._p.self_state.skills)}, "
                                f"feed={self._feed.total_count}")
        except Exception as e:
            self._log_error(f"tick error: {type(e).__name__}: {e}")

    def _log_error(self, msg: str) -> None:
        """Write debug message to data/anima-tui.log."""
        from pathlib import Path
        p = Path("data/anima-tui.log")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            from datetime import datetime as dt
            f.write(f"{dt.now().isoformat()} {msg}\n")
