"""Rich Live TUI — real-time terminal dashboard for Anima."""

from __future__ import annotations

import asyncio
import time
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
    t.append("\u2591" * (width - filled), style="grey30")
    t.append(f" {cur}/{mx}")
    return t


class AnimaTUI:
    """Real-time terminal dashboard using Rich Live display."""

    def __init__(
        self,
        perception: Perception,
        feed: ActivityFeed,
        blackboard: dict,
        refresh_rate: float = 0.5,
    ) -> None:
        self._p = perception
        self._feed = feed
        self._bb = blackboard
        self._refresh = refresh_rate
        self._start_time = time.time()

    def _build_status(self) -> Panel:
        ss = self._p.self_state
        persona = self._bb.get("persona")
        name = persona.name if persona else "Anima"
        goal = self._bb.get("current_goal")
        goal_text = goal.get("description", "")[:50] if goal else "none"

        elapsed = int(time.time() - self._start_time)
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60

        t = Text()
        t.append(f"{name}", style="bold bright_white")
        t.append(f"  [{h:02d}:{m:02d}:{s:02d}]\n\n", style="grey50")
        t.append("HP   ", style="bold red")
        t.append_text(_bar(ss.hits, ss.hits_max))
        t.append("  STR ", style="bold")
        t.append(f"{ss.strength}\n")
        t.append("Mana ", style="bold blue")
        t.append_text(_bar(ss.mana, ss.mana_max))
        t.append("  DEX ", style="bold")
        t.append(f"{ss.dexterity}\n")
        t.append("Stam ", style="bold yellow")
        t.append_text(_bar(ss.stam, ss.stam_max))
        t.append("  INT ", style="bold")
        t.append(f"{ss.intelligence}\n\n")
        t.append(f"Pos ({ss.x}, {ss.y}, {ss.z})  ", style="grey70")
        t.append(f"Gold {ss.gold:,}  ", style="bright_yellow")
        t.append(f"Wt {ss.weight}/{ss.weight_max}\n", style="grey70")
        t.append("Goal ", style="bright_green")
        t.append(goal_text)
        return Panel(t, title="Status", border_style="bright_blue")

    def _build_activity(self) -> Panel:
        events = self._feed.recent(18)
        t = Text()
        for ev in events:
            ts = datetime.fromtimestamp(ev.timestamp).strftime("%H:%M:%S")
            icon = CATEGORY_ICONS.get(ev.category, "\u2022")
            t.append(f" {ts} ", style="grey50")
            t.append(f"{icon} ")
            style = f"bold" if ev.importance >= 3 else ""
            t.append(f"{ev.message}\n", style=style)
        if not events:
            t.append(" Waiting for activity...", style="grey50")
        return Panel(t, title="Activity", border_style="bright_green")

    def _build_nearby(self) -> Panel:
        ss = self._p.self_state
        mobs = self._p.world.nearby_mobiles(ss.x, ss.y, distance=18)
        mobs.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))
        t = Text()
        for mob in mobs[:8]:
            name = (mob.name or f"0x{mob.body:04X}")[:18]
            dx, dy = mob.x - ss.x, mob.y - ss.y
            dirs = []
            if dy < 0: dirs.append(f"{abs(dy)}N")
            elif dy > 0: dirs.append(f"{abs(dy)}S")
            if dx > 0: dirs.append(f"{abs(dx)}E")
            elif dx < 0: dirs.append(f"{abs(dx)}W")
            nv = mob.notoriety.value if mob.notoriety else 1
            color = NOTORIETY_COLORS.get(nv, "white")
            t.append(f"{name}", style=color)
            t.append(f"  {','.join(dirs) or 'here'}\n", style="grey70")
        if not mobs:
            t.append("nobody nearby", style="grey50")
        return Panel(t, title="Nearby", border_style="bright_yellow")

    def _build_journal(self) -> Panel:
        entries = self._p.social.recent(count=10)
        my_serial = self._p.self_state.serial
        t = Text()
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
        return Panel(t, title="Journal", border_style="bright_magenta")

    def _build_inventory(self) -> Panel:
        ss = self._p.self_state
        bp = ss.equipment.get(0x15)
        t = Text()
        if bp:
            items = [it for it in self._p.world.items.values() if it.container == bp]
            items.sort(key=lambda it: it.name or f"0x{it.graphic:04X}")
            for it in items[:10]:
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

    def _build_skills(self) -> Panel:
        ss = self._p.self_state
        skills = sorted(ss.skills.values(), key=lambda s: (-s.value, s.id))
        t = Text()
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
            if count >= 12:
                break
        t.append(f"\nTotal {total:.1f}/700", style="bold")
        return Panel(t, title="Skills", border_style="bright_yellow")

    def _build_qvalues(self) -> Panel:
        q_snapshot: dict[str, tuple[float, int]] = self._bb.get("q_snapshot", {})
        t = Text()
        if q_snapshot:
            sorted_q = sorted(q_snapshot.items(), key=lambda x: x[1][0], reverse=True)
            for name, (q_val, visits) in sorted_q[:8]:
                color = "bright_green" if q_val > 0 else "red" if q_val < 0 else "grey70"
                t.append(f"{name[:16]:<16} ")
                t.append(f"Q={q_val:.2f} ", style=color)
                t.append(f"n={visits}\n", style="grey70")
        else:
            t.append("no data yet", style="grey50")
        return Panel(t, title="Q-Values", border_style="bright_cyan")

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="upper"),
            Layout(name="lower", size=14),
        )
        layout["upper"].split_row(
            Layout(name="status", ratio=2, minimum_size=30),
            Layout(name="activity", ratio=3, minimum_size=40),
        )
        layout["lower"].split_row(
            Layout(name="nearby", ratio=1),
            Layout(name="journal", ratio=1),
            Layout(name="inventory", ratio=1),
            Layout(name="skills", ratio=1),
            Layout(name="qvalues", ratio=1),
        )
        return layout

    async def run(self) -> None:
        console = Console()
        layout = self._build_layout()

        with Live(layout, console=console, refresh_per_second=2, screen=True):
            while True:
                try:
                    layout["status"].update(self._build_status())
                    layout["activity"].update(self._build_activity())
                    layout["nearby"].update(self._build_nearby())
                    layout["journal"].update(self._build_journal())
                    layout["inventory"].update(self._build_inventory())
                    layout["skills"].update(self._build_skills())
                    layout["qvalues"].update(self._build_qvalues())
                except Exception:
                    pass
                await asyncio.sleep(self._refresh)
