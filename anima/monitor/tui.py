"""Textual-based async TUI dashboard for Anima."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Static

if TYPE_CHECKING:
    from anima.monitor.feed import ActivityFeed
    from anima.perception import Perception

NOTORIETY_COLORS: dict[int, str] = {
    1: "dodger_blue1", 2: "green", 3: "grey70", 4: "grey70",
    5: "orange1", 6: "red", 7: "bright_yellow",
}

CATEGORY_ICONS: dict[str, str] = {
    "brain": "\u2b50", "skill": "\u2692", "combat": "\u2694",
    "movement": "\u2192", "social": "\U0001f4ac", "system": "\u2139",
}

LOCK_DISPLAY: dict[int, tuple[str, str]] = {
    0: ("\u2191", "green"), 1: ("\u2193", "red"), 2: ("\u2022", "grey50"),
}

SKILL_NAMES: dict[int, str] = {
    0: "Alchemy", 1: "Anatomy", 2: "Animal Lore", 3: "Item ID",
    4: "Arms Lore", 5: "Parrying", 7: "Blacksmith", 8: "Bowcraft",
    9: "Peacemaking", 11: "Carpentry", 13: "Cooking", 17: "Healing",
    18: "Fishing", 21: "Hiding", 22: "Provocation", 23: "Inscription",
    25: "Magery", 26: "Resist Spells", 27: "Tactics", 29: "Musicianship",
    31: "Archery", 34: "Tailoring", 35: "Taming", 37: "Tinkering",
    38: "Tracking", 39: "Veterinary", 40: "Swordsmanship",
    41: "Mace Fighting", 42: "Fencing", 43: "Wrestling",
    44: "Lumberjack", 45: "Mining", 46: "Meditation",
    47: "Stealth", 48: "Remove Trap",
}


def _bar(cur: int, mx: int, width: int = 10) -> Text:
    ratio = cur / mx if mx else 1.0
    filled = int(ratio * width)
    color = "red" if ratio < 0.25 else "yellow" if ratio < 0.5 else "green"
    t = Text()
    t.append("\u2588" * filled, style=color)
    t.append("\u2591" * (width - filled))
    t.append(f" {cur}/{mx}")
    return t


class AnimaTUI(App):
    """Textual-based async TUI for Anima."""

    TITLE = "Anima"
    CSS = """
    Screen { layout: vertical; }
    #upper { height: 1fr; }
    #lower { height: 16; }
    .box { border: round gray; padding: 0 1; }
    #status-box { width: 2fr; }
    #activity-box { width: 3fr; }
    #nearby-box { width: 1fr; }
    #journal-box { width: 1fr; }
    #inventory-box { width: 1fr; display: none; }
    #skills-box { width: 1fr; display: none; }
    #qvalues-box { width: 1fr; }
    """

    BINDINGS = [
        Binding("j", "toggle_journal", "Journal"),
        Binding("i", "toggle_inventory", "Inventory"),
        Binding("s", "toggle_skills", "Skills"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        perception: Perception,
        feed: ActivityFeed,
        blackboard: dict,
        refresh_rate: float = 0.5,
    ) -> None:
        super().__init__()
        self._p = perception
        self._feed = feed
        self._bb = blackboard
        self._refresh = refresh_rate

    def compose(self) -> ComposeResult:
        with Horizontal(id="upper"):
            yield Static("", id="status-box", classes="box")
            yield Static("", id="activity-box", classes="box")
        with Horizontal(id="lower"):
            yield Static("", id="nearby-box", classes="box")
            yield Static("", id="journal-box", classes="box")
            yield Static("", id="inventory-box", classes="box")
            yield Static("", id="skills-box", classes="box")
            yield Static("", id="qvalues-box", classes="box")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(self._refresh, self._refresh_all)

    def _refresh_all(self) -> None:
        try:
            self._refresh_status()
            self._refresh_activity()
            self._refresh_nearby()
            self._refresh_journal()
            self._refresh_inventory()
            self._refresh_skills()
            self._refresh_qvalues()
        except Exception as e:
            import structlog
            structlog.get_logger().warning("tui_error", error=str(e))

    def _refresh_status(self) -> None:
        ss = self._p.self_state
        persona = self._bb.get("persona")
        name = persona.name if persona else "Anima"
        title = getattr(persona, "title", "") if persona else ""
        goal = self._bb.get("current_goal")
        goal_text = goal.get("description", "")[:50] if goal else "none"

        t = Text()
        t.append(name, style="bold")
        t.append(f" — {title}\n\n")
        t.append("HP   ", style="bold red")
        t.append_text(_bar(ss.hits, ss.hits_max))
        t.append(f"  STR ", style="bold")
        t.append(f"{ss.strength}\n")
        t.append("Mana ", style="bold blue")
        t.append_text(_bar(ss.mana, ss.mana_max))
        t.append(f"  DEX ", style="bold")
        t.append(f"{ss.dexterity}\n")
        t.append("Stam ", style="bold yellow")
        t.append_text(_bar(ss.stam, ss.stam_max))
        t.append(f"  INT ", style="bold")
        t.append(f"{ss.intelligence}\n\n")
        t.append(f"Pos ({ss.x}, {ss.y}, {ss.z})  ", style="grey70")
        t.append(f"Gold {ss.gold:,}  ", style="bright_yellow")
        t.append(f"Wt {ss.weight}/{ss.weight_max}\n", style="grey70")
        t.append("Goal ", style="bright_green")
        t.append(goal_text)
        self.query_one("#status-box").update(t)

    def _refresh_activity(self) -> None:
        events = self._feed.recent(16)
        t = Text()
        t.append("Activity\n", style="bold")
        for ev in events:
            ts = datetime.fromtimestamp(ev.timestamp).strftime("%H:%M:%S")
            icon = CATEGORY_ICONS.get(ev.category, "\u2022")
            t.append(f"{ts} ", style="grey50")
            t.append(f"{icon} ")
            style = "bold" if ev.importance >= 3 else ""
            t.append(f"{ev.message}\n", style=style)
        if not events:
            t.append("Waiting...", style="grey50")
        self.query_one("#activity-box").update(t)

    def _refresh_nearby(self) -> None:
        ss = self._p.self_state
        mobs = self._p.world.nearby_mobiles(ss.x, ss.y, distance=18)
        mobs.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))
        t = Text()
        t.append("Nearby\n", style="bold")
        for mob in mobs[:10]:
            name = (mob.name or f"0x{mob.body:04X}")[:18]
            dx, dy = mob.x - ss.x, mob.y - ss.y
            dirs = []
            if dy < 0: dirs.append(f"{abs(dy)}N")
            elif dy > 0: dirs.append(f"{abs(dy)}S")
            if dx > 0: dirs.append(f"{abs(dx)}E")
            elif dx < 0: dirs.append(f"{abs(dx)}W")
            nv = mob.notoriety.value if mob.notoriety else 1
            color = NOTORIETY_COLORS.get(nv, "white")
            t.append(name, style=color)
            t.append(f" {','.join(dirs) or 'here'}\n", style="grey70")
        if not mobs:
            t.append("nobody nearby", style="grey50")
        self.query_one("#nearby-box").update(t)

    def _refresh_journal(self) -> None:
        entries = self._p.social.recent(count=12)
        my_serial = self._p.self_state.serial
        t = Text()
        t.append("Journal\n", style="bold")
        for entry in entries:
            ts = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M:%S")
            name = entry.name or "?"
            if entry.serial == my_serial:
                style = "bright_cyan"
            elif name.lower() == "system":
                style = "grey50"
            else:
                style = "bright_white"
            t.append(f"{ts} ", style="grey50")
            t.append(f"{name}: ", style=style)
            t.append(f"{entry.text[:55]}\n")
        if not entries:
            t.append("No speech yet...", style="grey50")
        self.query_one("#journal-box").update(t)

    def _refresh_inventory(self) -> None:
        ss = self._p.self_state
        bp = ss.equipment.get(0x15)
        t = Text()
        t.append("Inventory\n", style="bold")
        if bp:
            items = [it for it in self._p.world.items.values() if it.container == bp]
            items.sort(key=lambda it: it.name or f"0x{it.graphic:04X}")
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
        self.query_one("#inventory-box").update(t)

    def _refresh_skills(self) -> None:
        ss = self._p.self_state
        skills = sorted(ss.skills.values(), key=lambda s: (-s.value, s.id))
        t = Text()
        t.append("Skills\n", style="bold")
        total = 0.0
        count = 0
        for skill in skills:
            if skill.value == 0 and skill.lock.value == 2:
                continue
            total += skill.value
            name = SKILL_NAMES.get(skill.id, f"Skill {skill.id}")
            lv = skill.lock.value if hasattr(skill.lock, "value") else skill.lock
            icon, color = LOCK_DISPLAY.get(lv, ("?", "white"))
            t.append(f"{icon} ", style=color)
            t.append(f"{name[:14]:<14} ")
            t.append(f"{skill.value:5.1f}", style="bright_white")
            t.append(f"/{skill.cap:.0f}\n", style="grey50")
            count += 1
            if count >= 14:
                break
        t.append(f"\n  Total {total:.1f}/700", style="bold")
        self.query_one("#skills-box").update(t)

    def _refresh_qvalues(self) -> None:
        q_snapshot: dict[str, tuple[float, int]] = self._bb.get("q_snapshot", {})
        t = Text()
        t.append("Q-Values\n", style="bold")
        if q_snapshot:
            sorted_q = sorted(q_snapshot.items(), key=lambda x: x[1][0], reverse=True)
            for name, (q_val, visits) in sorted_q[:8]:
                color = "bright_green" if q_val > 0 else "red" if q_val < 0 else "grey70"
                t.append(f"{name[:16]:<16} ")
                t.append(f"Q={q_val:.2f} ", style=color)
                t.append(f"n={visits}\n", style="grey70")
        else:
            t.append("no data yet", style="grey50")
        self.query_one("#qvalues-box").update(t)

    def action_toggle_journal(self) -> None:
        box = self.query_one("#journal-box")
        box.display = not box.display

    def action_toggle_inventory(self) -> None:
        box = self.query_one("#inventory-box")
        box.display = not box.display

    def action_toggle_skills(self) -> None:
        box = self.query_one("#skills-box")
        box.display = not box.display

    async def run(self) -> None:  # type: ignore[override]
        """Run as asyncio coroutine."""
        await super().run_async()
