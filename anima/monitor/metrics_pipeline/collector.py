"""MetricsCollector — watches state.json, bus, and action_logs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from anima.monitor.metrics_pipeline import record

logger = structlog.get_logger()


class MetricsCollector:
    """Collects events from three sources into metrics_events.jsonl.

    Sources:
      1. state.json periodic diff (gold, skills, inventory, HP)
      2. EventBus subscriptions (expedition, death, auto_recover, phase)
      3. action_logs table polling (procedure_end events)
    """

    def __init__(self, events_file: Path | None = None) -> None:
        self.events_file = events_file
        self._last_state: dict | None = None
        self._was_dead: bool = False  # tracks hp==0 edge

    def _emit(self, event_type: str, **payload: Any) -> None:
        record(event_type, events_file=self.events_file, **payload)

    def _diff_state(self, prev: dict | None, curr: dict) -> None:
        """Compare two state.json snapshots and emit events for diffs."""
        if prev is None:
            # First snapshot — just remember it for next call.
            self._was_dead = _status(curr).get("hp", 1) <= 0
            return

        prev_s = _status(prev)
        curr_s = _status(curr)

        # Gold delta
        prev_gold = prev_s.get("gold", 0)
        curr_gold = curr_s.get("gold", 0)
        if curr_gold != prev_gold:
            self._emit("gold_delta", amount=curr_gold - prev_gold)

        # Death edge (hp falling to zero from >0)
        prev_hp = prev_s.get("hp", 0)
        curr_hp = curr_s.get("hp", 0)
        if curr_hp <= 0 and prev_hp > 0:
            pos = [curr_s.get("x", 0), curr_s.get("y", 0)]
            self._emit("death", pos=pos, hp_before=prev_hp)
            self._was_dead = True
        elif curr_hp > 0:
            self._was_dead = False

        # Skill deltas
        prev_skills = _skill_map(prev)
        curr_skills = _skill_map(curr)
        for sid, curr_val in curr_skills.items():
            prev_val = prev_skills.get(sid)
            if prev_val is not None and prev_val != curr_val:
                self._emit(
                    "skill_delta",
                    skill_id=sid,
                    **{"from": prev_val, "to": curr_val},
                )


def _status(snapshot: dict) -> dict:
    return snapshot.get("status") or {}


def _skill_map(snapshot: dict) -> dict[int, float]:
    """Extract {skill_id: value} from a state snapshot."""
    skills_block = snapshot.get("skills") or {}
    entries = skills_block.get("list") or []
    return {int(e["id"]): float(e["value"]) for e in entries if "id" in e and "value" in e}
