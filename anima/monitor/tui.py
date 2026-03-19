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
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from anima.monitor.feed import ActivityFeed
    from anima.perception import Perception

# Bar characters
BAR_FULL = "\u2588"  # █
BAR_EMPTY = "\u2591"  # ░

# Category display styles
CATEGORY_STYLES: dict[str, tuple[str, str]] = {
    # category -> (color, icon)
    "brain": ("bright_magenta", "\u2b50"),  # ⭐
    "skill": ("bright_cyan", "\u2692"),  # ⚒
    "combat": ("bright_red", "\u2694"),  # ⚔
    "movement": ("bright_green", "\u2192"),  # →
    "social": ("bright_yellow", "\U0001f4ac"),  # 💬
    "system": ("grey70", "\u2139"),  # ℹ
}

# Notoriety colors
NOTORIETY_COLORS: dict[int, str] = {
    1: "dodger_blue1",  # innocent
    2: "green",  # ally
    3: "grey70",  # attackable
    4: "grey70",  # criminal
    5: "orange1",  # enemy
    6: "red",  # murderer
    7: "bright_yellow",  # invulnerable
}


def _bar(current: int, maximum: int, width: int = 10, color: str = "green") -> Text:
    """Render a colored progress bar."""
    if maximum == 0:
        ratio = 1.0
    else:
        ratio = current / maximum
    filled = int(ratio * width)
    empty = width - filled

    # Color by ratio
    if ratio < 0.25:
        color = "red"
    elif ratio < 0.5:
        color = "yellow"

    bar = Text()
    bar.append(BAR_FULL * filled, style=color)
    bar.append(BAR_EMPTY * empty, style="grey30")
    bar.append(f" {current}/{maximum}", style="white")
    return bar


class AnimaTUI:
    """Real-time terminal dashboard using Rich Live display."""

    def __init__(
        self,
        perception: Perception,
        feed: ActivityFeed,
        blackboard: dict,
        refresh_rate: float = 0.5,
    ) -> None:
        self._perception = perception
        self._feed = feed
        self._bb = blackboard
        self._refresh_rate = refresh_rate
        self._start_time = time.time()
        self._show_journal = True
        self._show_inventory = False
        self._show_skills_list = False

    def _build_header(self) -> Text:
        persona = self._bb.get("persona")
        name = persona.name if persona else "Anima"
        title = getattr(persona, "title", "") if persona else ""

        elapsed = int(time.time() - self._start_time)
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60

        header = Text()
        header.append(f" {name}", style="bold bright_white")
        if title:
            header.append(f" — {title}", style="italic grey70")
        header.append(f"  [uptime {h:02d}:{m:02d}:{s:02d}]", style="grey50")
        return header

    def _build_status_panel(self) -> Panel:
        ss = self._perception.self_state
        goal = self._bb.get("current_goal")

        table = Table.grid(padding=(0, 2))
        table.add_column(width=6, justify="right")
        table.add_column()
        table.add_column(width=6, justify="right")
        table.add_column()

        table.add_row(
            Text("HP", style="bold red"),
            _bar(ss.hits, ss.hits_max, 10, "green"),
            Text("STR", style="bold"),
            Text(str(ss.strength)),
        )
        table.add_row(
            Text("Mana", style="bold blue"),
            _bar(ss.mana, ss.mana_max, 10, "blue"),
            Text("DEX", style="bold"),
            Text(str(ss.dexterity)),
        )
        table.add_row(
            Text("Stam", style="bold yellow"),
            _bar(ss.stam, ss.stam_max, 10, "yellow"),
            Text("INT", style="bold"),
            Text(str(ss.intelligence)),
        )
        table.add_row(Text(), Text())  # spacer
        table.add_row(
            Text("Pos", style="grey70"),
            Text(f"({ss.x}, {ss.y}, {ss.z})"),
            Text("Gold", style="bright_yellow"),
            Text(f"{ss.gold:,}"),
        )
        table.add_row(
            Text("Dir", style="grey70"),
            Text(f"{ss.direction & 0x07}"),
            Text("Wt", style="grey70"),
            Text(f"{ss.weight}/{ss.weight_max}"),
        )

        if goal:
            goal_text = Text()
            goal_text.append(goal.get("description", "")[:50], style="bright_white")
            table.add_row(Text("Goal", style="bright_green"), goal_text, Text(), Text())
        else:
            table.add_row(
                Text("Goal", style="grey50"), Text("none", style="grey50"), Text(), Text(),
            )

        return Panel(table, title="[bold]Status[/bold]", border_style="bright_blue")

    def _build_activity_panel(self) -> Panel:
        events = self._feed.recent(18)
        text = Text()

        for event in events:
            ts = datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S")
            style, icon = CATEGORY_STYLES.get(event.category, ("white", "\u2022"))

            # Highlight important events
            msg_style = style
            if event.importance >= 3:
                msg_style = f"bold {style}"

            text.append(f" {ts} ", style="grey50")
            text.append(f"{icon} ", style=style)
            text.append(f"{event.message}\n", style=msg_style)

        if not events:
            text.append(" Waiting for activity...", style="grey50")

        return Panel(text, title="[bold]Activity[/bold]", border_style="bright_green")

    def _build_nearby_panel(self) -> Panel:
        ss = self._perception.self_state
        mobiles = self._perception.world.nearby_mobiles(ss.x, ss.y, distance=18)

        table = Table.grid(padding=(0, 1))
        table.add_column(width=18)  # name
        table.add_column(width=8, justify="right")  # distance

        # Sort by distance
        mobiles.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))

        for mob in mobiles[:8]:
            dx, dy = mob.x - ss.x, mob.y - ss.y
            name = mob.name or f"0x{mob.body:04X}"

            not_val = mob.notoriety.value if mob.notoriety else 1
            color = NOTORIETY_COLORS.get(not_val, "white")

            # Direction indicator
            dirs = []
            if dy < 0:
                dirs.append(f"{abs(dy)}N")
            elif dy > 0:
                dirs.append(f"{abs(dy)}S")
            if dx > 0:
                dirs.append(f"{abs(dx)}E")
            elif dx < 0:
                dirs.append(f"{abs(dx)}W")
            dir_str = ",".join(dirs) if dirs else "here"

            table.add_row(
                Text(name[:18], style=color),
                Text(dir_str, style="grey70"),
            )

        if not mobiles:
            table.add_row(Text("nobody nearby", style="grey50"), Text())

        return Panel(table, title="[bold]Nearby[/bold]", border_style="bright_yellow")

    def _build_skills_panel(self) -> Panel:
        q_snapshot: dict[str, tuple[float, int]] = self._bb.get("q_snapshot", {})

        table = Table.grid(padding=(0, 1))
        table.add_column(width=16)  # skill name
        table.add_column(width=7, justify="right")  # Q-value
        table.add_column(width=5, justify="right")  # visits

        if q_snapshot:
            # Sort by Q-value descending
            sorted_q = sorted(q_snapshot.items(), key=lambda x: x[1][0], reverse=True)
            for skill_name, (q_val, visits) in sorted_q[:8]:
                q_color = "bright_green" if q_val > 0 else "red" if q_val < 0 else "grey70"
                table.add_row(
                    Text(skill_name[:16]),
                    Text(f"Q={q_val:.2f}", style=q_color),
                    Text(f"n={visits}", style="grey70"),
                )
        else:
            table.add_row(Text("no data yet", style="grey50"), Text(), Text())

        return Panel(table, title="[bold]Q-Values[/bold]", border_style="bright_cyan")

    def _build_journal_panel(self) -> Panel:
        """Show recent speech/journal entries."""
        entries = self._perception.social.recent(count=12)
        text = Text()

        my_serial = self._perception.self_state.serial

        for entry in entries:
            ts = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M:%S")
            name = entry.name or "?"

            if entry.serial == my_serial:
                style = "bright_cyan"
            elif name.lower() == "system":
                style = "grey50"
            else:
                style = "bright_white"

            text.append(f" {ts} ", style="grey50")
            text.append(f"{name}: ", style=f"bold {style}")
            text.append(f"{entry.text[:60]}\n", style=style)

        if not entries:
            text.append(" No speech yet...", style="grey50")

        return Panel(text, title="[bold]Journal[/bold]", border_style="bright_magenta")

    def _build_inventory_panel(self) -> Panel:
        """Show backpack contents."""
        ss = self._perception.self_state
        world = self._perception.world
        backpack = ss.equipment.get(0x15)

        table = Table.grid(padding=(0, 1))
        table.add_column(width=20)  # name
        table.add_column(width=6, justify="right")  # amount

        if backpack:
            items = [
                it for it in world.items.values()
                if it.container == backpack
            ]
            items.sort(key=lambda it: it.name or f"0x{it.graphic:04X}")
            for it in items[:12]:
                name = it.name or f"0x{it.graphic:04X}"
                amt = f"x{it.amount}" if it.amount > 1 else ""
                table.add_row(Text(name[:20]), Text(amt, style="grey70"))

            if not items:
                table.add_row(Text("empty", style="grey50"), Text())
        else:
            table.add_row(Text("no backpack", style="grey50"), Text())

        return Panel(table, title="[bold]Inventory[/bold]", border_style="bright_white")

    def _build_skills_list_panel(self) -> Panel:
        """Show character skills with values and lock states."""
        ss = self._perception.self_state
        lock_icons = {0: "\u2191", 1: "\u2193", 2: "\u2022"}  # ↑ ↓ •
        lock_colors = {0: "bright_green", 1: "red", 2: "grey50"}

        # Skill name lookup (common ones)
        SKILL_NAMES = {
            0: "Alchemy", 1: "Anatomy", 2: "Animal Lore", 3: "Item ID",
            4: "Arms Lore", 5: "Parrying", 7: "Blacksmith", 8: "Bowcraft",
            9: "Peacemaking", 11: "Carpentry", 13: "Cooking", 17: "Healing",
            18: "Fishing", 21: "Hiding", 22: "Provocation", 23: "Inscription",
            25: "Magery", 26: "Resist Spells", 27: "Tactics", 29: "Musicianship",
            31: "Archery", 34: "Tailoring", 35: "Taming", 37: "Tinkering",
            38: "Tracking", 39: "Veterinary", 40: "Swordsmanship",
            41: "Mace Fighting", 42: "Fencing", 43: "Wrestling",
            44: "Lumberjacking", 45: "Mining", 46: "Meditation",
            47: "Stealth", 48: "Remove Trap",
        }

        table = Table.grid(padding=(0, 1))
        table.add_column(width=2)   # lock icon
        table.add_column(width=15)  # name
        table.add_column(width=6, justify="right")  # value
        table.add_column(width=6, justify="right")  # cap

        # Sort: non-zero skills first by value descending, then rest
        skills = sorted(
            ss.skills.values(),
            key=lambda s: (-s.value, s.id),
        )

        shown = 0
        total = sum(s.value for s in skills)
        for skill in skills:
            if shown >= 16:
                break
            if skill.value == 0 and skill.lock.value == 2:
                continue  # skip zero+locked
            name = SKILL_NAMES.get(skill.id, f"Skill {skill.id}")
            lock_val = skill.lock.value if hasattr(skill.lock, 'value') else skill.lock
            icon = lock_icons.get(lock_val, "?")
            color = lock_colors.get(lock_val, "white")
            table.add_row(
                Text(icon, style=color),
                Text(name[:15]),
                Text(f"{skill.value:.1f}", style="bright_white"),
                Text(f"/{skill.cap:.0f}", style="grey50"),
            )
            shown += 1

        if shown == 0:
            table.add_row(Text(), Text("no skills yet", style="grey50"), Text(), Text())
        else:
            table.add_row(Text(), Text(), Text(), Text())
            table.add_row(
                Text(), Text("Total", style="bold"), Text(f"{total:.1f}", style="bold"), Text(),
            )

        return Panel(table, title="[bold]Skills[/bold]", border_style="bright_yellow")

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=1),
            Layout(name="body"),
        )
        layout["body"].split_column(
            Layout(name="upper"),
            Layout(name="lower", size=14),
        )
        layout["upper"].split_row(
            Layout(name="status", ratio=2, minimum_size=30),
            Layout(name="activity", ratio=3, minimum_size=40),
        )

        # Build lower row based on toggle state
        lower_panels = [Layout(name="nearby", ratio=1)]
        if self._show_journal:
            lower_panels.append(Layout(name="journal", ratio=1))
        if self._show_inventory:
            lower_panels.append(Layout(name="inventory", ratio=1))
        if self._show_skills_list:
            lower_panels.append(Layout(name="skills_list", ratio=1))
        lower_panels.append(Layout(name="skills", ratio=1))
        layout["lower"].split_row(*lower_panels)

        return layout

    def _handle_key(self, key: str) -> bool:
        """Handle keyboard input. Returns True if layout needs rebuild."""
        if key == "j":
            self._show_journal = not self._show_journal
            return True
        if key == "i":
            self._show_inventory = not self._show_inventory
            return True
        if key == "s":
            self._show_skills_list = not self._show_skills_list
            return True
        return False

    async def _poll_keys(self) -> str | None:
        """Non-blocking key read from stdin."""
        import sys
        import select
        if select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1)
        return None

    async def run(self) -> None:
        import sys
        import tty
        import termios

        console = Console()
        layout = self._build_layout()

        # Set terminal to raw mode for key capture
        old_settings = termios.tcgetattr(sys.stdin)
        try:
            tty.setcbreak(sys.stdin.fileno())

            with Live(layout, console=console, refresh_per_second=2, screen=True):
                while True:
                    try:
                        # Check for key input
                        key = await self._poll_keys()
                        if key and self._handle_key(key):
                            layout = self._build_layout()

                        layout["header"].update(self._build_header())
                        layout["status"].update(self._build_status_panel())
                        layout["activity"].update(self._build_activity_panel())
                        layout["nearby"].update(self._build_nearby_panel())
                        if self._show_journal:
                            layout["journal"].update(self._build_journal_panel())
                        if self._show_inventory:
                            layout["inventory"].update(self._build_inventory_panel())
                        if self._show_skills_list:
                            layout["skills_list"].update(self._build_skills_list_panel())
                        layout["skills"].update(self._build_skills_panel())
                    except Exception:
                        pass  # Never crash the TUI
                    await asyncio.sleep(self._refresh_rate)
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
