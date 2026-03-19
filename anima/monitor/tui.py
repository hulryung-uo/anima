"""Textual-based async TUI dashboard for Anima."""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Footer, Header, Static

if TYPE_CHECKING:
    from anima.monitor.feed import ActivityFeed
    from anima.perception import Perception

# Notoriety colors (Rich markup)
NOTORIETY_COLORS: dict[int, str] = {
    1: "dodger_blue1", 2: "green", 3: "grey70", 4: "grey70",
    5: "orange1", 6: "red", 7: "bright_yellow",
}

CATEGORY_ICONS: dict[str, str] = {
    "brain": "\u2b50", "skill": "\u2692", "combat": "\u2694",
    "movement": "\u2192", "social": "\U0001f4ac", "system": "\u2139",
}

LOCK_DISPLAY: dict[int, tuple[str, str]] = {
    0: ("\u2191", "green"),    # ↑ Up
    1: ("\u2193", "red"),      # ↓ Down
    2: ("\u2022", "grey50"),   # • Locked
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
    if mx == 0:
        ratio = 1.0
    else:
        ratio = cur / mx
    filled = int(ratio * width)
    color = "red" if ratio < 0.25 else "yellow" if ratio < 0.5 else "green"
    return f"[{color}]{'\u2588' * filled}[/]{'\u2591' * (width - filled)} {cur}/{mx}"


class StatusWidget(Static):
    def compose(self) -> ComposeResult:
        yield Static(id="status-content")

    def refresh_data(self, perception: Perception, blackboard: dict) -> None:
        ss = perception.self_state
        goal = blackboard.get("current_goal")
        goal_text = goal.get("description", "")[:50] if goal else "[grey50]none[/]"

        content = (
            f"[bold red]HP[/]   {_bar(ss.hits, ss.hits_max)}  "
            f"[bold]STR[/] {ss.strength}\n"
            f"[bold blue]Mana[/] {_bar(ss.mana, ss.mana_max)}  "
            f"[bold]DEX[/] {ss.dexterity}\n"
            f"[bold yellow]Stam[/] {_bar(ss.stam, ss.stam_max)}  "
            f"[bold]INT[/] {ss.intelligence}\n"
            f"\n"
            f"[grey70]Pos[/] ({ss.x}, {ss.y}, {ss.z})  "
            f"[bright_yellow]Gold[/] {ss.gold:,}  "
            f"[grey70]Wt[/] {ss.weight}/{ss.weight_max}\n"
            f"[bright_green]Goal[/] {goal_text}"
        )
        self.query_one("#status-content").update(content)


class ActivityWidget(Static):
    def compose(self) -> ComposeResult:
        yield Static(id="activity-content")

    def refresh_data(self, feed: ActivityFeed) -> None:
        events = feed.recent(18)
        lines: list[str] = []
        for event in events:
            ts = datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S")
            icon = CATEGORY_ICONS.get(event.category, "\u2022")
            style = "bold " if event.importance >= 3 else ""
            lines.append(f"[grey50]{ts}[/] {icon} [{style}]{event.message}[/]")
        self.query_one("#activity-content").update(
            "\n".join(lines) if lines else "[grey50]Waiting for activity...[/]"
        )


class NearbyWidget(Static):
    def compose(self) -> ComposeResult:
        yield Static(id="nearby-content")

    def refresh_data(self, perception: Perception) -> None:
        ss = perception.self_state
        mobs = perception.world.nearby_mobiles(ss.x, ss.y, distance=18)
        mobs.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))

        lines: list[str] = []
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
            lines.append(f"[{color}]{name}[/]  [grey70]{','.join(dirs) or 'here'}[/]")

        self.query_one("#nearby-content").update(
            "\n".join(lines) if lines else "[grey50]nobody nearby[/]"
        )


class JournalWidget(Static):
    def compose(self) -> ComposeResult:
        yield Static(id="journal-content")

    def refresh_data(self, perception: Perception) -> None:
        entries = perception.social.recent(count=14)
        my_serial = perception.self_state.serial
        lines: list[str] = []
        for entry in entries:
            ts = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M:%S")
            name = entry.name or "?"
            if entry.serial == my_serial:
                style = "bright_cyan"
            elif name.lower() == "system":
                style = "grey50"
            else:
                style = "bright_white"
            lines.append(f"[grey50]{ts}[/] [{style}]{name}:[/] {entry.text[:60]}")

        self.query_one("#journal-content").update(
            "\n".join(lines) if lines else "[grey50]No speech yet...[/]"
        )


class InventoryWidget(Static):
    def compose(self) -> ComposeResult:
        yield Static(id="inv-content")

    def refresh_data(self, perception: Perception) -> None:
        ss = perception.self_state
        world = perception.world
        bp = ss.equipment.get(0x15)
        lines: list[str] = []
        if bp:
            items = [it for it in world.items.values() if it.container == bp]
            items.sort(key=lambda it: it.name or f"0x{it.graphic:04X}")
            for it in items[:14]:
                name = it.name or f"0x{it.graphic:04X}"
                amt = f" x{it.amount}" if it.amount > 1 else ""
                lines.append(f"{name[:20]}[grey70]{amt}[/]")
        self.query_one("#inv-content").update(
            "\n".join(lines) if lines else "[grey50]empty[/]"
        )


class SkillsWidget(Static):
    def compose(self) -> ComposeResult:
        yield Static(id="skills-content")

    def refresh_data(self, perception: Perception) -> None:
        ss = perception.self_state
        skills = sorted(ss.skills.values(), key=lambda s: (-s.value, s.id))
        lines: list[str] = []
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
            if len(lines) >= 14:
                break
        lines.append(f"\n  [bold]Total {total:.1f}/700[/]")
        self.query_one("#skills-content").update(
            "\n".join(lines) if lines else "[grey50]no skills yet[/]"
        )


class QValuesWidget(Static):
    def compose(self) -> ComposeResult:
        yield Static(id="qvalues-content")

    def refresh_data(self, blackboard: dict) -> None:
        q_snapshot: dict[str, tuple[float, int]] = blackboard.get("q_snapshot", {})
        lines: list[str] = []
        if q_snapshot:
            sorted_q = sorted(q_snapshot.items(), key=lambda x: x[1][0], reverse=True)
            for skill_name, (q_val, visits) in sorted_q[:8]:
                color = "bright_green" if q_val > 0 else "red" if q_val < 0 else "grey70"
                lines.append(
                    f"{skill_name[:16]:<16} [{color}]Q={q_val:.2f}[/] [grey70]n={visits}[/]"
                )
        self.query_one("#qvalues-content").update(
            "\n".join(lines) if lines else "[grey50]no data yet[/]"
        )


class AnimaTUI(App):
    """Textual-based async TUI for Anima."""

    TITLE = "Anima"
    CSS = """
    #upper { height: 1fr; }
    #lower { height: 14; }
    .panel { border: solid gray; padding: 0 1; }
    #status-panel { width: 2fr; min-width: 30; }
    #activity-panel { width: 3fr; min-width: 40; }
    #nearby-panel { width: 1fr; }
    #journal-panel { width: 1fr; }
    #inventory-panel { width: 1fr; display: none; }
    #skills-panel { width: 1fr; display: none; }
    #qvalues-panel { width: 1fr; }
    """

    BINDINGS = [
        Binding("j", "toggle_journal", "Journal"),
        Binding("i", "toggle_inventory", "Inventory"),
        Binding("s", "toggle_skills", "Skills"),
        Binding("q", "quit", "Quit"),
    ]

    show_journal = reactive(True)
    show_inventory = reactive(False)
    show_skills = reactive(False)

    def __init__(
        self,
        perception: Perception,
        feed: ActivityFeed,
        blackboard: dict,
        refresh_rate: float = 0.5,
    ) -> None:
        super().__init__()
        self._perception = perception
        self._feed = feed
        self._bb = blackboard
        self._refresh_rate = refresh_rate
        self._start_time = time.time()

    def compose(self) -> ComposeResult:
        persona = self._bb.get("persona")
        name = persona.name if persona else "Anima"
        title = getattr(persona, "title", "") if persona else ""
        yield Header(show_clock=True)
        with Vertical():
            with Horizontal(id="upper"):
                with Vertical(id="status-panel", classes="panel"):
                    yield Static(f"[bold]{name}[/] — {title}", id="persona-title")
                    yield StatusWidget(id="status-widget")
                with Vertical(id="activity-panel", classes="panel"):
                    yield Static("[bold]Activity[/]")
                    yield ActivityWidget(id="activity-widget")
            with Horizontal(id="lower"):
                with Vertical(id="nearby-panel", classes="panel"):
                    yield Static("[bold]Nearby[/]")
                    yield NearbyWidget(id="nearby-widget")
                with Vertical(id="journal-panel", classes="panel"):
                    yield Static("[bold]Journal[/]")
                    yield JournalWidget(id="journal-widget")
                with Vertical(id="inventory-panel", classes="panel"):
                    yield Static("[bold]Inventory[/]")
                    yield InventoryWidget(id="inv-widget")
                with Vertical(id="skills-panel", classes="panel"):
                    yield Static("[bold]Skills[/]")
                    yield SkillsWidget(id="skills-widget")
                with Vertical(id="qvalues-panel", classes="panel"):
                    yield Static("[bold]Q-Values[/]")
                    yield QValuesWidget(id="qvalues-widget")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(self._refresh_rate, self._update_all)

    def _update_all(self) -> None:
        try:
            self.query_one("#status-widget", StatusWidget).refresh_data(
                self._perception, self._bb
            )
            self.query_one("#activity-widget", ActivityWidget).refresh_data(self._feed)
            self.query_one("#nearby-widget", NearbyWidget).refresh_data(self._perception)
            self.query_one("#journal-widget", JournalWidget).refresh_data(self._perception)
            self.query_one("#inv-widget", InventoryWidget).refresh_data(self._perception)
            self.query_one("#skills-widget", SkillsWidget).refresh_data(self._perception)
            self.query_one("#qvalues-widget", QValuesWidget).refresh_data(self._bb)
        except Exception:
            pass

    def action_toggle_journal(self) -> None:
        panel = self.query_one("#journal-panel")
        panel.display = not panel.display

    def action_toggle_inventory(self) -> None:
        panel = self.query_one("#inventory-panel")
        panel.display = not panel.display

    def action_toggle_skills(self) -> None:
        panel = self.query_one("#skills-panel")
        panel.display = not panel.display

    async def run(self) -> None:  # type: ignore[override]
        """Run Textual app as an asyncio coroutine."""
        await super().run_async()
