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
        self._action_log_checkpoint: float = 0.0

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

    # --- Bus subscriptions ---

    def attach_bus(self, bus) -> None:
        """Subscribe to agent events; call once after construction."""
        bus.subscribe("expedition.cycle_complete", self._on_cycle)
        bus.subscribe("expedition.phase", self._on_phase)
        bus.subscribe("planner.death", self._on_death_event)
        bus.subscribe("supervisor.auto_recover", self._on_auto_recover)

    def _on_cycle(self, _topic: str, data: dict) -> None:
        self._emit(
            "cycle_complete",
            cycles_completed=data.get("cycles", 0),
            duration_s=data.get("duration_s", 0.0),
        )

    def _on_phase(self, _topic: str, data: dict) -> None:
        self._emit(
            "phase_transition",
            from_=data.get("from_"),
            to=data.get("to"),
        )

    def _on_death_event(self, _topic: str, data: dict) -> None:
        self._emit(
            "death",
            source="bus",
            message=(data.get("message") or "")[:60],
        )

    def _on_auto_recover(self, _topic: str, data: dict) -> None:
        self._emit("stuck_event", reason=data.get("reason", ""))

    # --- action_logs polling ---

    async def poll_action_logs(
        self, *, db_path: Path | None = None, since: float | None = None,
    ) -> int:
        """Emit procedure_end events for rows newer than last checkpoint.

        Returns the number of new events emitted.
        """
        import aiosqlite

        if db_path is None:
            db_path = Path("data/anima.db")
        cutoff = since if since is not None else self._action_log_checkpoint

        count = 0
        max_seen = cutoff
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT timestamp, procedure, result, duration_ms "
                "FROM action_logs WHERE timestamp > ? ORDER BY timestamp",
                (cutoff,),
            )
            async for ts, proc, result, dur in cursor:
                self._emit(
                    "procedure_end",
                    proc=proc,
                    success=(result == "success"),
                    duration_ms=float(dur or 0.0),
                    result=result,
                )
                count += 1
                if ts > max_seen:
                    max_seen = ts

        self._action_log_checkpoint = max_seen
        return count


def _status(snapshot: dict) -> dict:
    return snapshot.get("status") or {}


def _skill_map(snapshot: dict) -> dict[int, float]:
    """Extract {skill_id: value} from a state snapshot."""
    skills_block = snapshot.get("skills") or {}
    entries = skills_block.get("list") or []
    return {int(e["id"]): float(e["value"]) for e in entries if "id" in e and "value" in e}
