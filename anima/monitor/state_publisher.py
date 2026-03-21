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

if TYPE_CHECKING:
    from anima.core.bus import EventBus
    from anima.perception import Perception

STATE_FILE = Path("data/state.json")


class StatePublisher:
    """Reads Perception + blackboard and publishes snapshots to EventBus."""

    def __init__(
        self,
        perception: Perception,
        blackboard: dict[str, Any],
        bus: EventBus,
    ) -> None:
        self._p = perception
        self._bb = blackboard
        self._bus = bus
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
            name = (mob.name or f"0x{mob.body:04X}")[:18]
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
                    "name": (m.name or f"0x{m.body:04X}")[:18],
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
        }

        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot))
            tmp.replace(STATE_FILE)  # atomic on POSIX
        except Exception:
            pass
