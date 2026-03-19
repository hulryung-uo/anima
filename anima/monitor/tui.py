"""Textual TUI dashboard for Anima.

Architecture: Textual App owns the event loop. Game coroutines
are launched as asyncio.Tasks inside on_mount().

Layout: CSS Grid with 3 columns x 3 rows.
Key bindings: j=Journal, i=Inventory, s=Skills, q=Quit
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, Coroutine

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
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
    ratio = cur / mx if mx else 1.0
    filled = int(ratio * width)
    color = "red" if ratio < 0.25 else "yellow" if ratio < 0.5 else "green"
    t = Text()
    t.append("\u2588" * filled, style=color)
    t.append("\u2591" * (width - filled), style="grey30")
    t.append(f" {cur}/{mx}")
    return t


# ---------------------------------------------------------------------------
# Pure render functions → Rich Text
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
        ("HP  ", "bold red", ss.hits, ss.hits_max, "STR", ss.strength),
        ("Mana", "bold blue", ss.mana, ss.mana_max, "DEX", ss.dexterity),
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
    events = feed.recent(20)
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
        for name, (q, v) in sorted(
            qs.items(), key=lambda x: x[1][0], reverse=True
        )[:8]:
            c = "bright_green" if q > 0 else "red" if q < 0 else "grey70"
            t.append(f"{name[:16]:<16} ")
            t.append(f"Q={q:.2f} ", style=c)
            t.append(f"n={v}\n", style="grey70")
    else:
        t.append("no data yet", style="grey50")
    return t


# ---------------------------------------------------------------------------
# Textual App — CSS Grid layout
# ---------------------------------------------------------------------------

class AnimaTUI(App):
    """Anima TUI dashboard. Owns the event loop."""

    TITLE = "Anima"

    CSS = """
    Screen {
        layout: grid;
        grid-size: 3 3;
        grid-columns: 2fr 2fr 1fr;
        grid-rows: 1fr 1fr 1;
        grid-gutter: 0;
    }
    .panel {
        border: round gray;
        padding: 0 1;
        overflow-y: auto;
    }
    #p-status {
        column-span: 1;
    }
    #p-activity {
        column-span: 2;
    }
    #p-nearby {
        column-span: 1;
    }
    #p-journal {
        column-span: 1;
    }
    #p-qvalues {
        column-span: 1;
    }
    Footer {
        column-span: 3;
    }
    """

    BINDINGS = [
        Binding("j", "toggle_journal", "Journal"),
        Binding("i", "toggle_inventory", "Inventory"),
        Binding("s", "toggle_skills", "Skills"),
        Binding("q", "quit", "Quit"),
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

    def compose(self) -> ComposeResult:
        yield Static("Loading...", id="p-status", classes="panel")
        yield Static("Activity", id="p-activity", classes="panel")
        yield Static("Nearby", id="p-nearby", classes="panel")
        yield Static("Journal", id="p-journal", classes="panel")
        yield Static("Q-Values", id="p-qvalues", classes="panel")
        yield Footer()

    def on_mount(self) -> None:
        for coro in self._bg_coros:
            self._tasks.append(asyncio.create_task(coro))
        self.set_interval(self._rate, self._tick)

    def on_key(self, event) -> None:
        key = event.character
        if key == "j":
            self.action_toggle_journal()
        elif key == "i":
            self.action_toggle_inventory()
        elif key == "s":
            self.action_toggle_skills()

    async def action_quit(self) -> None:
        for task in self._tasks:
            task.cancel()
        self.exit()

    def action_toggle_journal(self) -> None:
        w = self.query_one("#p-journal")
        w.display = not w.display

    def action_toggle_inventory(self) -> None:
        """Toggle inventory panel — replaces journal when shown."""
        journal = self.query_one("#p-journal")
        inv = self.query("#p-inventory")
        if inv:
            inv.first().remove()
            journal.display = True
        else:
            journal.display = False
            self.mount(
                Static(_render_inventory(self._p), id="p-inventory", classes="panel"),
                after=self.query_one("#p-nearby"),
            )

    def action_toggle_skills(self) -> None:
        """Toggle skills panel — replaces qvalues when shown."""
        qvals = self.query_one("#p-qvalues")
        sk = self.query("#p-skills")
        if sk:
            sk.first().remove()
            qvals.display = True
        else:
            qvals.display = False
            self.mount(
                Static(_render_skills(self._p), id="p-skills", classes="panel"),
                after=self.query_one("#p-qvalues"),
            )

    def _tick(self) -> None:
        try:
            self.query_one("#p-status").update(_render_status(self._p, self._bb))
            self.query_one("#p-activity").update(_render_activity(self._feed))
            self.query_one("#p-nearby").update(_render_nearby(self._p))

            journal = self.query("#p-journal")
            if journal:
                journal.first().update(_render_journal(self._p))

            inv = self.query("#p-inventory")
            if inv:
                inv.first().update(_render_inventory(self._p))

            qvals = self.query("#p-qvalues")
            if qvals:
                qvals.first().update(_render_qvalues(self._bb))

            sk = self.query("#p-skills")
            if sk:
                sk.first().update(_render_skills(self._p))
        except Exception:
            pass
