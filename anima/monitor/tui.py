"""Textual-based async TUI dashboard for Anima."""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
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


def _bar(cur: int, mx: int, width: int = 10) -> str:
    ratio = cur / mx if mx else 1.0
    filled = int(ratio * width)
    color = "red" if ratio < 0.25 else "yellow" if ratio < 0.5 else "green"
    return f"[{color}]{'\u2588' * filled}[/]{'\u2591' * (width - filled)} {cur}/{mx}"


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

        text = (
            f"[bold]{name}[/] — {title}\n\n"
            f"[bold red]HP[/]   {_bar(ss.hits, ss.hits_max)}  "
            f"[bold]STR[/] {ss.strength}\n"
            f"[bold blue]Mana[/] {_bar(ss.mana, ss.mana_max)}  "
            f"[bold]DEX[/] {ss.dexterity}\n"
            f"[bold yellow]Stam[/] {_bar(ss.stam, ss.stam_max)}  "
            f"[bold]INT[/] {ss.intelligence}\n\n"
            f"[grey70]Pos[/] ({ss.x}, {ss.y}, {ss.z})  "
            f"[bright_yellow]Gold[/] {ss.gold:,}  "
            f"[grey70]Wt[/] {ss.weight}/{ss.weight_max}\n"
            f"[bright_green]Goal[/] {goal_text}"
        )
        self.query_one("#status-box").update(text)

    def _refresh_activity(self) -> None:
        events = self._feed.recent(16)
        lines = ["[bold]Activity[/]\n"]
        for ev in events:
            ts = datetime.fromtimestamp(ev.timestamp).strftime("%H:%M:%S")
            icon = CATEGORY_ICONS.get(ev.category, "\u2022")
            bold = "bold " if ev.importance >= 3 else ""
            lines.append(f"[grey50]{ts}[/] {icon} [{bold}]{ev.message}[/]")
        self.query_one("#activity-box").update(
            "\n".join(lines) if len(lines) > 1 else "[grey50]Waiting...[/]"
        )

    def _refresh_nearby(self) -> None:
        ss = self._p.self_state
        mobs = self._p.world.nearby_mobiles(ss.x, ss.y, distance=18)
        mobs.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))
        lines = ["[bold]Nearby[/]\n"]
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
            lines.append(f"[{color}]{name}[/] [grey70]{','.join(dirs) or 'here'}[/]")
        if len(lines) == 1:
            lines.append("[grey50]nobody nearby[/]")
        self.query_one("#nearby-box").update("\n".join(lines))

    def _refresh_journal(self) -> None:
        entries = self._p.social.recent(count=12)
        my_serial = self._p.self_state.serial
        lines = ["[bold]Journal[/]\n"]
        for entry in entries:
            ts = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M:%S")
            name = entry.name or "?"
            if entry.serial == my_serial:
                style = "bright_cyan"
            elif name.lower() == "system":
                style = "grey50"
            else:
                style = "bright_white"
            lines.append(
                f"[grey50]{ts}[/] [{style}]{name}:[/] {entry.text[:55]}"
            )
        if len(lines) == 1:
            lines.append("[grey50]No speech yet...[/]")
        self.query_one("#journal-box").update("\n".join(lines))

    def _refresh_inventory(self) -> None:
        ss = self._p.self_state
        bp = ss.equipment.get(0x15)
        lines = ["[bold]Inventory[/]\n"]
        if bp:
            items = [it for it in self._p.world.items.values() if it.container == bp]
            items.sort(key=lambda it: it.name or f"0x{it.graphic:04X}")
            for it in items[:12]:
                name = it.name or f"0x{it.graphic:04X}"
                amt = f" x{it.amount}" if it.amount > 1 else ""
                lines.append(f"{name[:20]}[grey70]{amt}[/]")
        if len(lines) == 1:
            lines.append("[grey50]empty[/]")
        self.query_one("#inventory-box").update("\n".join(lines))

    def _refresh_skills(self) -> None:
        ss = self._p.self_state
        skills = sorted(ss.skills.values(), key=lambda s: (-s.value, s.id))
        lines = ["[bold]Skills[/]\n"]
        total = 0.0
        for skill in skills:
            if skill.value == 0 and skill.lock.value == 2:
                continue
            total += skill.value
            name = SKILL_NAMES.get(skill.id, f"Skill {skill.id}")
            lv = skill.lock.value if hasattr(skill.lock, "value") else skill.lock
            icon, color = LOCK_DISPLAY.get(lv, ("?", "white"))
            lines.append(
                f"[{color}]{icon}[/] {name[:14]:<14} "
                f"[bright_white]{skill.value:5.1f}[/][grey50]/{skill.cap:.0f}[/]"
            )
            if len(lines) >= 15:
                break
        lines.append(f"\n  [bold]Total {total:.1f}/700[/]")
        self.query_one("#skills-box").update("\n".join(lines))

    def _refresh_qvalues(self) -> None:
        q_snapshot: dict[str, tuple[float, int]] = self._bb.get("q_snapshot", {})
        lines = ["[bold]Q-Values[/]\n"]
        if q_snapshot:
            sorted_q = sorted(q_snapshot.items(), key=lambda x: x[1][0], reverse=True)
            for name, (q_val, visits) in sorted_q[:8]:
                color = "bright_green" if q_val > 0 else "red" if q_val < 0 else "grey70"
                lines.append(f"{name[:16]:<16} [{color}]Q={q_val:.2f}[/] [grey70]n={visits}[/]")
        if len(lines) == 1:
            lines.append("[grey50]no data yet[/]")
        self.query_one("#qvalues-box").update("\n".join(lines))

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
