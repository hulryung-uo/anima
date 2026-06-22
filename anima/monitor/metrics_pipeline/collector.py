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

    # A real UO death is a ghost state held until a healer resurrection (many
    # seconds); the vitals stream flickers during it (a stale 0x11/0x16 hits
    # update can briefly read >0 before dropping back to 0). Treat a recovery
    # to less than this fraction of hp_max as that flicker, NOT a resurrection,
    # so the death-edge stays debounced and one death is counted once.
    _ALIVE_RECOVERY_FRACTION = 0.10

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
        #
        # Only the FIRST hits<=0 sample of a death is a death. While ``_was_dead``
        # is latched, a hits>0 reading is treated as a transient stale-vitals
        # flicker, not a resurrection, unless hp recovers to a meaningful share
        # of hp_max -- so a ghost-state flicker (e.g. 0 -> 1 -> 0) does NOT clear
        # the latch and the relapse to 0 cannot fire a SECOND death. Without this
        # the hourly/daily ``deaths`` rollup and the death alert over-count one
        # death as many. Mirrors foundry apprentice.analyze_deaths' MIN_DEATH_S
        # transient-blip discipline for the live monitor.
        prev_hp = prev_s.get("hp", 0)
        curr_hp = curr_s.get("hp", 0)
        if curr_hp <= 0 and prev_hp > 0 and not self._was_dead:
            pos = [curr_s.get("x", 0), curr_s.get("y", 0)]
            self._emit("death", pos=pos, hp_before=prev_hp)
            self._was_dead = True
        elif curr_hp > 0:
            hp_max = curr_s.get("hp_max", 0) or 0
            # A genuine resurrection restores real HP; a 1-hp flicker does not.
            recovered = curr_hp >= max(2, hp_max * self._ALIVE_RECOVERY_FRACTION)
            if recovered:
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
        # The bus (planner.death) and the state.json hp<=0 edge are TWO
        # producers for the SAME real death: the planner publishes here in the
        # same loop iteration that drives hits<=0 into state.json. Counting both
        # double-counts one death in the hourly/daily ``deaths`` rollup and
        # doubles the death alert's value. Gate the bus death through the same
        # ``_was_dead`` latch the state-diff path uses, so whichever producer
        # observes the death first wins and the other is suppressed until a real
        # resurrection clears the latch (see ``_diff_state``).
        if self._was_dead:
            return
        self._was_dead = True
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
            # Created lazily by the first procedure log write — a fresh DB
            # has no table yet; that's "no data", not a poll error.
            probe = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='action_logs'"
            )
            if await probe.fetchone() is None:
                return 0
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
    # ``state.json`` is read off disk every poll (``_state_diff_loop``) and can
    # be a torn concurrent write or be shaped by a foreign/future producer, so an
    # entry is NOT guaranteed to be a ``{"id", "value"}`` dict with numeric
    # fields. The old comprehension only guarded the KEY presence
    # (``"id" in e and "value" in e``) and still:
    #   * raised ``TypeError`` on a non-container entry (``"id" in None`` /
    #     ``"id" in 7`` — "argument of type ... is not iterable"), and
    #   * raised ``ValueError`` / ``TypeError`` on a non-numeric id/value
    #     (``int("x")`` / ``float(None)``).
    # The state-diff loop catches the exception and retries every 5s against the
    # SAME unchanged snapshot, so one malformed skills entry doesn't crash the
    # session — it silently blackouts the whole metrics state-diff (gold / death /
    # skill deltas) for as long as that entry persists. Skip a malformed entry the
    # same way the rest of the metrics pipeline skips a torn/non-object event line
    # (``_read_events_in_window`` / ``trim_events`` / ``_coerce_amount``), so a
    # good entry beside a bad one still produces its delta.
    out: dict[int, float] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        sid = e.get("id")
        val = e.get("value")
        if sid is None or val is None:
            continue
        try:
            out[int(sid)] = float(val)
        except (TypeError, ValueError):
            continue
    return out
