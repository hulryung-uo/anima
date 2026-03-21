"""StatePublisher — periodically publishes state snapshots to EventBus + file.

Decouples monitors from direct Perception/blackboard access.
Monitors subscribe to ``monitor.*`` topics to receive state updates.

Also writes a JSON snapshot to ``data/state.json`` so that an external
TUI process can read it without sharing memory.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anima.data import mobile_display_name

if TYPE_CHECKING:
    from anima.core.bus import EventBus
    from anima.map import MapReader
    from anima.perception import Perception

STATE_FILE = Path("data/state.json")


class StatePublisher:
    """Reads Perception + blackboard and publishes snapshots to EventBus."""

    def __init__(
        self,
        perception: Perception,
        blackboard: dict[str, Any],
        bus: EventBus,
        map_reader: MapReader | None = None,
    ) -> None:
        self._p = perception
        self._bb = blackboard
        self._bus = bus
        self._map_reader = map_reader
        self._activity: list[dict] = []
        # Collect activity events from bus
        bus.subscribe("*", self._collect_activity)

    def _collect_activity(self, topic: str, data: dict[str, Any]) -> None:
        if topic.startswith("monitor."):
            return
        message = data.get("message", "")
        if not message:
            return
        self._activity.append({
            "ts": time.time(),
            "topic": topic,
            "message": message,
            "importance": data.get("importance", 1),
        })
        # Keep bounded
        if len(self._activity) > 200:
            self._activity = self._activity[-200:]

    async def run(self, interval: float = 0.5) -> None:
        """Publish state snapshots in a loop."""
        while True:
            self.publish_all()
            self._dump_to_file()
            await asyncio.sleep(interval)

    def publish_all(self) -> None:
        self._publish_status()
        self._publish_nearby()
        self._publish_journal()
        self._publish_inventory()
        self._publish_skills()
        self._publish_qvalues()

    # ------------------------------------------------------------------

    def _publish_status(self) -> None:
        ss = self._p.self_state
        persona = self._bb.get("persona")
        goal = self._bb.get("current_goal")
        move_target = self._bb.get("move_target")
        self._bus.publish("monitor.status", {
            "name": persona.name if persona else "Anima",
            "title": getattr(persona, "title", "") if persona else "",
            "hp": ss.hits, "hp_max": ss.hits_max,
            "mana": ss.mana, "mana_max": ss.mana_max,
            "stam": ss.stam, "stam_max": ss.stam_max,
            "str": ss.strength, "dex": ss.dexterity, "int": ss.intelligence,
            "x": ss.x, "y": ss.y, "z": ss.z,
            "gold": ss.gold,
            "weight": ss.weight, "weight_max": ss.weight_max,
            "goal": goal.get("description", "")[:50] if goal else "none",
            "move_target": list(move_target) if move_target else None,
        })

    def _publish_nearby(self) -> None:
        ss = self._p.self_state
        mobs = self._p.world.nearby_mobiles(ss.x, ss.y, distance=18)
        mobs.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))
        mob_data = []
        for mob in mobs[:8]:
            name = mobile_display_name(mob)[:18]
            nv = mob.notoriety.value if mob.notoriety else 1
            mob_data.append({
                "name": name,
                "x": mob.x, "y": mob.y,
                "dx": mob.x - ss.x, "dy": mob.y - ss.y,
                "notoriety": nv,
            })
        self._bus.publish("monitor.nearby", {"mobiles": mob_data})

    def _publish_journal(self) -> None:
        entries = self._p.social.recent(count=10)
        my_serial = self._p.self_state.serial
        journal_data = []
        for e in entries:
            journal_data.append({
                "timestamp": e.timestamp,
                "name": e.name or "?",
                "serial": e.serial,
                "text": e.text[:55],
                "is_self": e.serial == my_serial,
            })
        self._bus.publish("monitor.journal", {
            "entries": journal_data,
            "my_serial": my_serial,
        })

    def _publish_inventory(self) -> None:
        ss = self._p.self_state
        bp = ss.equipment.get(0x15)
        items_data = []
        if bp:
            items = sorted(
                [it for it in self._p.world.items.values() if it.container == bp],
                key=lambda it: it.name or "",
            )
            for it in items[:12]:
                name = it.name or f"0x{it.graphic:04X}"
                items_data.append({"name": name[:20], "amount": it.amount})
        self._bus.publish("monitor.inventory", {
            "items": items_data,
            "has_backpack": bp is not None,
        })

    def _publish_skills(self) -> None:
        skills = sorted(
            self._p.self_state.skills.values(),
            key=lambda s: (-s.value, s.id),
        )
        skills_data = []
        total = 0.0
        for sk in skills:
            if sk.value == 0 and sk.lock.value == 2:
                continue
            total += sk.value
            skills_data.append({
                "id": sk.id,
                "value": sk.value,
                "cap": sk.cap,
                "lock": sk.lock.value,
            })
        self._bus.publish("monitor.skills", {
            "skills": skills_data[:12],
            "total": total,
        })

    def _publish_qvalues(self) -> None:
        qs: dict = self._bb.get("q_snapshot", {})
        qv_data = {}
        for name, (q, v) in sorted(
            qs.items(), key=lambda x: x[1][0], reverse=True,
        )[:8]:
            qv_data[name] = {"q": q, "visits": v}
        self._bus.publish("monitor.qvalues", {"values": qv_data})

    def _build_minimap(self, ss: object, mobs: list) -> dict:
        """Build a minimap grid around the player position."""
        from anima.map import FLAG_DOOR, FLAG_IMPASSABLE

        radius = 30  # generate wide map, TUI trims to fit panel
        px = getattr(ss, "x", 0)
        py = getattr(ss, "y", 0)

        if not self._map_reader:
            return {"rows": [], "px": px, "py": py, "radius": radius}

        # Build mob positions for overlay
        mob_positions: dict[tuple[int, int], str] = {}
        for m in mobs:
            if getattr(m, "serial", 0) == getattr(ss, "serial", -1):
                continue
            mob_positions[(m.x, m.y)] = "M"

        # Build world item positions (doors, etc.)
        item_positions: dict[tuple[int, int], str] = {}
        for it in self._p.world.items.values():
            if it.container != 0:
                continue
            if abs(it.x - px) <= radius and abs(it.y - py) <= radius:
                flags = self._map_reader._get_item_flags(it.graphic)
                if flags & FLAG_DOOR:
                    item_positions[(it.x, it.y)] = "+"

        # Goal marker
        move_target = self._bb.get("move_target")
        goal_pos = None
        if move_target:
            goal_pos = (move_target[0], move_target[1])

        rows: list[str] = []
        for dy in range(-radius, radius + 1):
            row = ""
            y = py + dy
            for dx in range(-radius, radius + 1):
                x = px + dx
                if dx == 0 and dy == 0:
                    row += "@"
                elif goal_pos and x == goal_pos[0] and y == goal_pos[1]:
                    row += "X"
                elif (x, y) in mob_positions:
                    row += "M"
                elif (x, y) in item_positions:
                    row += "+"
                else:
                    tile = self._map_reader.get_tile(x, y)
                    has_wall = any(
                        s.flags & FLAG_IMPASSABLE for s in tile.statics
                    )
                    has_tree = any(
                        s.graphic in range(0x0CCA, 0x0CCF)
                        or s.graphic in range(0x0CD0, 0x0CD9)
                        for s in tile.statics
                    )
                    if has_wall:
                        row += "#"
                    elif has_tree:
                        row += "T"
                    else:
                        row += "."
            rows.append(row)

        return {"rows": rows, "px": px, "py": py, "radius": radius}

    def _dump_to_file(self) -> None:
        """Write full state snapshot to data/state.json for external TUI."""
        ss = self._p.self_state
        persona = self._bb.get("persona")
        goal = self._bb.get("current_goal")
        move_target = self._bb.get("move_target")

        # Collect all panels into one dict
        mobs = self._p.world.nearby_mobiles(ss.x, ss.y, distance=18)
        mobs.sort(key=lambda m: abs(m.x - ss.x) + abs(m.y - ss.y))

        entries = self._p.social.recent(count=10)
        my_serial = ss.serial

        bp = ss.equipment.get(0x15)
        items = []
        if bp:
            for it in sorted(
                [i for i in self._p.world.items.values() if i.container == bp],
                key=lambda i: i.name or "",
            )[:12]:
                items.append({
                    "name": (it.name or f"0x{it.graphic:04X}")[:20],
                    "amount": it.amount,
                })

        skills = sorted(self._p.self_state.skills.values(), key=lambda s: -s.value)
        skills_data = []
        total_skill = 0.0
        for sk in skills:
            if sk.value == 0 and sk.lock.value == 2:
                continue
            total_skill += sk.value
            skills_data.append({
                "id": sk.id, "value": sk.value,
                "cap": sk.cap, "lock": sk.lock.value,
            })

        qs: dict = self._bb.get("q_snapshot", {})
        qv = {}
        for name, (q, v) in sorted(
            qs.items(), key=lambda x: x[1][0], reverse=True,
        )[:8]:
            qv[name] = {"q": round(q, 1), "visits": v}

        snapshot = {
            "ts": time.time(),
            "status": {
                "name": persona.name if persona else "Anima",
                "hp": ss.hits, "hp_max": ss.hits_max,
                "mana": ss.mana, "mana_max": ss.mana_max,
                "stam": ss.stam, "stam_max": ss.stam_max,
                "str": ss.strength, "dex": ss.dexterity,
                "int": ss.intelligence,
                "x": ss.x, "y": ss.y, "z": ss.z,
                "gold": ss.gold,
                "weight": ss.weight, "weight_max": ss.weight_max,
                "goal": goal.get("description", "")[:50] if goal else "",
                "move_target": list(move_target) if move_target else None,
            },
            "nearby": [
                {
                    "name": mobile_display_name(m)[:18],
                    "x": m.x, "y": m.y,
                    "dx": m.x - ss.x, "dy": m.y - ss.y,
                    "notoriety": m.notoriety.value if m.notoriety else 1,
                }
                for m in mobs[:8]
            ],
            "journal": [
                {
                    "ts": e.timestamp, "name": e.name or "?",
                    "text": e.text[:55],
                    "is_self": e.serial == my_serial,
                }
                for e in entries
            ],
            "inventory": items,
            "skills": {"list": skills_data[:12], "total": round(total_skill, 1)},
            "qvalues": qv,
            "activity": self._activity[-30:],
            "minimap": self._build_minimap(ss, mobs),
        }

        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot))
            tmp.replace(STATE_FILE)  # atomic on POSIX
        except Exception:
            pass
