"""MetricsAggregator — hourly and daily rollups + retention trim."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

logger = structlog.get_logger()


class MetricsAggregator:
    def __init__(
        self,
        *,
        events_file: Path,
        hourly_file: Path,
        daily_file: Path,
        db_path: Path,
        bus=None,
    ) -> None:
        self.events_file = events_file
        self.hourly_file = hourly_file
        self.daily_file = daily_file
        self.db_path = db_path
        self.bus = bus

    # ------------------------------------------------------------------
    # Hourly rollup
    # ------------------------------------------------------------------

    async def build_hourly(
        self, *, window_start: float, window_end: float,
    ) -> dict:
        """Build and persist one hourly row for [window_start, window_end)."""
        events = _read_events_in_window(
            self.events_file, window_start, window_end,
        )
        procs = await _aggregate_procedures(self.db_path, window_start, window_end)

        earned = sum(
            int(e["amount"]) for e in events
            if e["type"] == "gold_delta" and e.get("amount", 0) > 0
        )
        spent = sum(
            -int(e["amount"]) for e in events
            if e["type"] == "gold_delta" and e.get("amount", 0) < 0
        )

        hour_iso = (
            datetime.fromtimestamp(window_start, tz=timezone.utc)
            .replace(minute=0, second=0, microsecond=0)
            .isoformat()
        )

        row: dict[str, Any] = {
            "hour": hour_iso,
            "uptime_s": int(window_end - window_start),
            "procedures": procs,
            "cycles_completed": sum(
                1 for e in events if e["type"] == "cycle_complete"
            ),
            "phase_transitions": _count_phase_transitions(events),
            "gold": {
                "earned": earned,
                "spent": spent,
                "delta": earned - spent,
            },
            "deaths": sum(1 for e in events if e["type"] == "death"),
            "stuck_events": sum(
                1 for e in events if e["type"] == "stuck_event"
            ),
            "skills": _aggregate_skill_deltas(events),
        }

        _append_jsonl(self.hourly_file, row)
        if self.bus is not None:
            # Supervisor is a separate process; it cannot subscribe to this
            # in-process bus. state_publisher captures any bus event with a
            # `message` field into state.json's activity feed, which the
            # supervisor DOES read. Include a one-line summary alongside
            # the full row so both worlds work.
            hour_short = (row["hour"] or "")[:16].replace("T", " ")
            proc_stats = row.get("procedures", {})
            ok = sum(p.get("ok", 0) for p in proc_stats.values())
            fail = sum(p.get("fail", 0) for p in proc_stats.values())
            rate = (ok / (ok + fail)) if (ok + fail) else 1.0
            summary = (
                f"HOUR {hour_short}: cycles={row.get('cycles_completed', 0)} "
                f"gold+={row['gold'].get('delta', 0)} "
                f"deaths={row.get('deaths', 0)} "
                f"stuck={row.get('stuck_events', 0)} "
                f"proc_ok={rate:.0%}"
            )
            try:
                self.bus.publish("metrics.hourly_complete", {
                    "message": summary,
                    "importance": 2,
                    "row": row,
                })
            except Exception as e:
                logger.warning("metrics_hourly_publish_failed", error=str(e))
        return row

    # ------------------------------------------------------------------
    # Daily rollup
    # ------------------------------------------------------------------

    async def build_daily(self, *, date_iso: str) -> dict:
        """Aggregate all hourly rows whose hour starts with date_iso."""
        hourly_rows = _read_hourly_rows_for_date(self.hourly_file, date_iso)

        cycles_total = sum(r.get("cycles_completed", 0) for r in hourly_rows)
        gold_earned = sum(r.get("gold", {}).get("earned", 0) for r in hourly_rows)
        gold_spent = sum(r.get("gold", {}).get("spent", 0) for r in hourly_rows)
        deaths = sum(r.get("deaths", 0) for r in hourly_rows)
        stuck_events = sum(r.get("stuck_events", 0) for r in hourly_rows)
        uptime_s = sum(r.get("uptime_s", 0) for r in hourly_rows)

        total_ok = 0
        total_fail = 0
        fail_by_proc: dict[str, int] = defaultdict(int)
        for r in hourly_rows:
            for proc, stats in r.get("procedures", {}).items():
                total_ok += stats.get("ok", 0)
                total_fail += stats.get("fail", 0)
                fail_by_proc[proc] += stats.get("fail", 0)
        total_runs = total_ok + total_fail
        success_rate = (total_ok / total_runs) if total_runs else 0.0

        top_failures = sorted(
            fail_by_proc.items(), key=lambda kv: (-kv[1], kv[0]),
        )[:5]

        # Skills gained = accumulate per-hour deltas (to - from for each row)
        skills_gained: dict[str, float] = {}
        for r in hourly_rows:
            for sid, s in r.get("skills", {}).items():
                if sid not in skills_gained:
                    skills_gained[sid] = 0.0
                # Accumulate per-hour deltas
                skills_gained[sid] += float(
                    (s.get("to") or 0) - (s.get("from") or 0)
                )

        row: dict[str, Any] = {
            "date": date_iso,
            "uptime_s": uptime_s,
            "cycles_total": cycles_total,
            "gold_earned": gold_earned,
            "gold_spent": gold_spent,
            "net_gold": gold_earned - gold_spent,
            "deaths": deaths,
            "stuck_events": stuck_events,
            "procedure_success_rate": round(success_rate, 4),
            "top_failures": [[p, c] for p, c in top_failures],
            "skills_gained": {k: round(v, 2) for k, v in skills_gained.items()},
            "auto_recover_count": stuck_events,  # today these are the same
            "hourly_missing": len(hourly_rows) == 0,
        }

        _append_jsonl(self.daily_file, row)
        if self.bus is not None:
            try:
                self.bus.publish("metrics.daily_complete", row)
            except Exception as e:
                logger.warning("metrics_daily_publish_failed", error=str(e))
        return row

    # ------------------------------------------------------------------
    # Retention trim
    # ------------------------------------------------------------------

    async def trim_events(self, *, cutoff_ts: float) -> int:
        """Rewrite events file, dropping rows with ts < cutoff_ts.

        Returns the number of rows removed.
        """
        if not self.events_file.exists():
            return 0
        kept: list[str] = []
        removed = 0
        for line in self.events_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                kept.append(line)
                continue
            if obj.get("ts", 0) < cutoff_ts:
                removed += 1
            else:
                kept.append(line)

        if removed == 0:
            return 0

        tmp = self.events_file.with_suffix(".jsonl.tmp")
        tmp.write_text(("\n".join(kept) + "\n") if kept else "")
        tmp.replace(self.events_file)
        logger.info("metrics_trim_complete", removed=removed, kept=len(kept))
        return removed


def _read_events_in_window(
    events_file: Path, start: float, end: float,
) -> list[dict]:
    if not events_file.exists():
        return []
    out = []
    for line in events_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        ts = obj.get("ts", 0)
        if start <= ts < end:
            out.append(obj)
    return out


async def _aggregate_procedures(
    db_path: Path, start: float, end: float,
) -> dict[str, dict]:
    procs: dict[str, dict] = defaultdict(
        lambda: {"ok": 0, "fail": 0, "durations": []}
    )
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT procedure, result, duration_ms FROM action_logs "
            "WHERE timestamp >= ? AND timestamp < ?",
            (start, end),
        )
        async for proc, result, dur in cursor:
            slot = procs[proc]
            if result == "success":
                slot["ok"] += 1
            else:
                slot["fail"] += 1
            if dur is not None:
                slot["durations"].append(float(dur))

    # Replace durations list with avg_ms
    out: dict[str, dict] = {}
    for proc, slot in procs.items():
        durs = slot["durations"]
        avg = (sum(durs) / len(durs)) if durs else 0.0
        out[proc] = {"ok": slot["ok"], "fail": slot["fail"], "avg_ms": round(avg, 1)}
    return out


def _count_phase_transitions(events: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for e in events:
        if e["type"] != "phase_transition":
            continue
        key = f"{e.get('from_', '?')}->{e.get('to', '?')}"
        counts[key] += 1
    return dict(counts)


def _aggregate_skill_deltas(events: list[dict]) -> dict[str, dict]:
    per_skill: dict[int, dict] = {}
    for e in events:
        if e["type"] != "skill_delta":
            continue
        sid = e.get("skill_id")
        if sid is None:
            continue
        slot = per_skill.setdefault(sid, {"from": e.get("from"), "to": e.get("to")})
        # Keep the earliest "from" and the latest "to"
        if e.get("ts", 0) < slot.get("_earliest_ts", float("inf")):
            slot["from"] = e.get("from")
        slot["to"] = e.get("to")
        slot["_earliest_ts"] = e.get("ts", 0)
    # Strip internal bookkeeping
    for slot in per_skill.values():
        slot.pop("_earliest_ts", None)
    return {str(sid): slot for sid, slot in per_skill.items()}


def _read_hourly_rows_for_date(path: Path, date_iso: str) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        hour = obj.get("hour", "")
        if hour.startswith(date_iso):
            out.append(obj)
    return out


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
