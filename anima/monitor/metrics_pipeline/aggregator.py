"""MetricsAggregator — hourly and daily rollups + retention trim."""

from __future__ import annotations

import json
import time
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
            if e.get("type") == "gold_delta" and e.get("amount", 0) > 0
        )
        spent = sum(
            -int(e["amount"]) for e in events
            if e.get("type") == "gold_delta" and e.get("amount", 0) < 0
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
                1 for e in events if e.get("type") == "cycle_complete"
            ),
            "phase_transitions": _count_phase_transitions(events),
            "gold": {
                "earned": earned,
                "spent": spent,
                "delta": earned - spent,
            },
            "deaths": sum(1 for e in events if e.get("type") == "death"),
            "stuck_events": sum(
                1 for e in events if e.get("type") == "stuck_event"
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
        # No procedure runs in the whole day = "no data", NOT a 0% (total
        # failure) success rate. The hourly summary (build_hourly: the
        # ``rate`` line) and the alert path (alerts._success_rate) both treat
        # an empty denominator as 1.0 for exactly this reason — defaulting the
        # daily rollup to 0.0 instead made an agent-down / non-procedure day
        # (or a fresh DB with no action_logs) read as a catastrophic 0% in the
        # daily JSONL that the dashboard and tools/metrics.py consume, and it
        # would also trip the low_success_rate alert if it were ever fed back.
        # Align the empty-window default across all three producers.
        success_rate = (total_ok / total_runs) if total_runs else 1.0

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
    # Scheduling / tick loop
    # ------------------------------------------------------------------

    _MAX_BACKFILL_HOURS = 6

    async def run_once(self, *, now: float | None = None) -> int:
        """Fire any rollups whose boundaries have passed since the last run.

        Returns the number of hourly rows written in this call (including
        backfills).
        """
        if now is None:
            now = time.time()

        # Align `now` down to the current hour boundary
        current_hour_start = now - (now % 3600)

        last_iso = _last_hourly_iso(self.hourly_file)
        if last_iso:
            # next_start is the beginning of the first hour NOT yet written
            next_start = _parse_hour_iso(last_iso) + 3600
        else:
            # No prior hourly rows — backfill from one hour ago
            next_start = current_hour_start - 3600

        written = 0
        attempted = 0
        while next_start + 3600 <= current_hour_start and attempted < self._MAX_BACKFILL_HOURS:
            await self.build_hourly(
                window_start=next_start, window_end=next_start + 3600,
            )
            written += 1
            next_start += 3600
            attempted += 1

        # Daily rollup once per UTC midnight
        yesterday = (
            datetime.fromtimestamp(now - 86400, tz=timezone.utc).date().isoformat()
        )
        if _should_build_daily(self.daily_file, yesterday):
            await self.build_daily(date_iso=yesterday)
            cutoff = now - 30 * 86400  # 30-day retention
            await self.trim_events(cutoff_ts=cutoff)

        return written

    async def run_forever(self) -> None:
        """Background task — call run_once() every 60s."""
        import asyncio
        while True:
            try:
                await self.run_once()
            except Exception as e:
                logger.error("metrics_run_once_failed", error=str(e))
            await asyncio.sleep(60)

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
            # A non-object JSON line (bare number/string/list/null) has no
            # ``ts`` to compare and would crash ``obj.get`` — and the trim is
            # exactly what would otherwise PURGE such a wedging line. Keep it
            # verbatim (like an unparseable line) rather than crash the trim.
            if not isinstance(obj, dict):
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
        # A torn concurrent append (or any other producer) can leave a line
        # that is valid JSON but NOT an object — a bare number/string/list/
        # null. ``obj.get`` would then raise AttributeError, aborting the
        # whole rollup; since run_once() only advances past an hour after a
        # SUCCESSFUL build_hourly, one such line wedges the hourly/daily
        # pipeline forever (retrying the same crash every 60s). Skip it, the
        # same way an unparseable line above is skipped.
        if not isinstance(obj, dict):
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
        # The table is created lazily by the first procedure log write; a
        # fresh (or per-eval) DB doesn't have it yet. Missing table = no
        # procedure data, not an error worth spamming every poll.
        probe = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='action_logs'"
        )
        if await probe.fetchone() is None:
            return {}
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
        if e.get("type") != "phase_transition":
            continue
        key = f"{e.get('from_', '?')}->{e.get('to', '?')}"
        counts[key] += 1
    return dict(counts)


def _aggregate_skill_deltas(events: list[dict]) -> dict[str, dict]:
    per_skill: dict[int, dict] = {}
    for e in events:
        if e.get("type") != "skill_delta":
            continue
        sid = e.get("skill_id")
        if sid is None:
            continue
        ts = e.get("ts", 0)
        slot = per_skill.get(sid)
        if slot is None:
            per_skill[sid] = {
                "from": e.get("from"), "to": e.get("to"),
                "_earliest_ts": ts, "_latest_ts": ts,
            }
            continue
        # Keep the earliest "from" and the latest "to" *by timestamp*.
        # Raw events are read in file/append order, but several concurrent
        # producers (state-diff poll, bus callbacks, action-log poll) write
        # the same stream, so list order is not guaranteed monotonic in ts.
        # Picking from/to by ts (not by position) keeps the per-skill delta
        # (to - from) faithful to the actual before->after of the window.
        if ts < slot["_earliest_ts"]:
            slot["from"] = e.get("from")
            slot["_earliest_ts"] = ts
        if ts >= slot["_latest_ts"]:
            slot["to"] = e.get("to")
            slot["_latest_ts"] = ts
    # Strip internal bookkeeping
    for slot in per_skill.values():
        slot.pop("_earliest_ts", None)
        slot.pop("_latest_ts", None)
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


def _last_hourly_iso(path: Path) -> str | None:
    """Return the ``hour`` field of the last valid line in path, or None."""
    if not path.exists():
        return None
    last = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last = json.loads(line).get("hour")
        except Exception:
            continue
    return last


def _parse_hour_iso(iso: str) -> float:
    """Parse an ISO-8601 hour string to a UTC epoch float. Returns 0.0 on error."""
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


def _should_build_daily(daily_file: Path, date_iso: str) -> bool:
    """Return True if date_iso is not already present in daily_file."""
    if not daily_file.exists():
        return True
    for line in daily_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            if json.loads(line).get("date") == date_iso:
                return False  # already built
        except Exception:
            continue
    return True
