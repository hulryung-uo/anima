"""Tests for MetricsAggregator."""
from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from anima.monitor.metrics_pipeline.aggregator import MetricsAggregator


@pytest.fixture
def events_file(tmp_path: Path) -> Path:
    return tmp_path / "metrics_events.jsonl"


@pytest.fixture
def hourly_file(tmp_path: Path) -> Path:
    return tmp_path / "metrics_hourly.jsonl"


@pytest.fixture
def daily_file(tmp_path: Path) -> Path:
    return tmp_path / "metrics_daily.jsonl"


def _write_events(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


async def _make_db(path: Path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE action_logs (
                timestamp REAL, agent TEXT, procedure TEXT,
                location_x INTEGER, location_y INTEGER,
                result TEXT, message TEXT, duration_ms REAL,
                details TEXT
            )
        """)
        await db.commit()


async def _insert_log(path: Path, ts: float, proc: str, result: str, dur_ms: float) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT INTO action_logs VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, "Grimm", proc, 0, 0, result, "", dur_ms, "{}"),
        )
        await db.commit()


class TestHourlyRollup:
    @pytest.mark.asyncio
    async def test_counts_gold_deltas_and_cycles(
        self, events_file, hourly_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        _write_events(events_file, [
            {"ts": 100.0, "type": "gold_delta", "amount": 50},
            {"ts": 150.0, "type": "gold_delta", "amount": -10},
            {"ts": 200.0, "type": "cycle_complete", "cycles_completed": 1, "duration_s": 100.0},
            {"ts": 250.0, "type": "death", "pos": [0, 0], "hp_before": 10},
            {"ts": 300.0, "type": "stuck_event", "reason": "idle"},
        ])

        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=tmp_path / "d.jsonl",
            db_path=db,
        )
        row = await agg.build_hourly(window_start=0.0, window_end=3600.0)

        assert row["gold"]["earned"] == 50
        assert row["gold"]["spent"] == 10
        assert row["gold"]["delta"] == 40
        assert row["cycles_completed"] == 1
        assert row["deaths"] == 1
        assert row["stuck_events"] == 1

    @pytest.mark.asyncio
    async def test_aggregates_procedures_from_action_logs(
        self, events_file, hourly_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        await _insert_log(db, 100.0, "mine_ore", "success", 3500.0)
        await _insert_log(db, 200.0, "mine_ore", "success", 3700.0)
        await _insert_log(db, 300.0, "mine_ore", "blocked", 1000.0)
        await _insert_log(db, 400.0, "smelt_ore", "success", 2000.0)

        events_file.write_text("")
        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=tmp_path / "d.jsonl",
            db_path=db,
        )
        row = await agg.build_hourly(window_start=0.0, window_end=3600.0)

        assert row["procedures"]["mine_ore"]["ok"] == 2
        assert row["procedures"]["mine_ore"]["fail"] == 1
        assert row["procedures"]["mine_ore"]["avg_ms"] == pytest.approx(2733.3, rel=0.01)
        assert row["procedures"]["smelt_ore"]["ok"] == 1
        assert row["procedures"]["smelt_ore"]["fail"] == 0

    @pytest.mark.asyncio
    async def test_appends_hourly_row_to_file(
        self, events_file, hourly_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        events_file.write_text("")

        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=tmp_path / "d.jsonl",
            db_path=db,
        )
        await agg.build_hourly(window_start=0.0, window_end=3600.0)
        await agg.build_hourly(window_start=3600.0, window_end=7200.0)
        lines = hourly_file.read_text().splitlines()
        assert len(lines) == 2
        rows = [json.loads(l) for l in lines]
        # Second row window differs from first
        assert rows[0]["hour"] != rows[1]["hour"]

    @pytest.mark.asyncio
    async def test_window_filters_events_outside_range(
        self, events_file, hourly_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        _write_events(events_file, [
            {"ts": 50.0,   "type": "gold_delta", "amount": 100},   # before window
            {"ts": 1800.0, "type": "gold_delta", "amount": 30},    # in window
            {"ts": 4000.0, "type": "gold_delta", "amount": 200},   # after window
        ])

        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=tmp_path / "d.jsonl",
            db_path=db,
        )
        row = await agg.build_hourly(window_start=1000.0, window_end=3600.0)
        assert row["gold"]["earned"] == 30


class TestDailyRollup:
    @pytest.mark.asyncio
    async def test_sums_hourly_rows_for_a_date(
        self, events_file, hourly_file, daily_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        # Write two pre-existing hourly rows for 2026-04-17
        hourly_rows = [
            {
                "hour": "2026-04-17T00:00:00+00:00",
                "uptime_s": 3600,
                "procedures": {"mine_ore": {"ok": 10, "fail": 2, "avg_ms": 3500}},
                "cycles_completed": 1,
                "phase_transitions": {},
                "gold": {"earned": 50, "spent": 20, "delta": 30},
                "deaths": 0,
                "stuck_events": 1,
                "skills": {"7": {"from": 63.0, "to": 63.2}},
            },
            {
                "hour": "2026-04-17T01:00:00+00:00",
                "uptime_s": 3600,
                "procedures": {
                    "mine_ore": {"ok": 8, "fail": 0, "avg_ms": 3400},
                    "smelt_ore": {"ok": 3, "fail": 1, "avg_ms": 2000},
                },
                "cycles_completed": 2,
                "phase_transitions": {},
                "gold": {"earned": 80, "spent": 10, "delta": 70},
                "deaths": 1,
                "stuck_events": 0,
                "skills": {"7": {"from": 63.2, "to": 63.5}},
            },
        ]
        hourly_file.write_text("\n".join(json.dumps(r) for r in hourly_rows) + "\n")

        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=daily_file,
            db_path=db,
        )
        row = await agg.build_daily(date_iso="2026-04-17")

        assert row["date"] == "2026-04-17"
        assert row["cycles_total"] == 3
        assert row["gold_earned"] == 130
        assert row["gold_spent"] == 30
        assert row["net_gold"] == 100
        assert row["deaths"] == 1
        assert row["stuck_events"] == 1
        # 21 ok / 3 fail → success_rate 0.875
        assert row["procedure_success_rate"] == pytest.approx(0.875, rel=0.01)
        # Top failures: mine_ore 2, smelt_ore 1
        assert row["top_failures"][0] == ["mine_ore", 2]
        assert row["skills_gained"]["7"] == pytest.approx(0.5, rel=0.01)

    @pytest.mark.asyncio
    async def test_missing_hourly_flags_hourly_missing(
        self, events_file, hourly_file, daily_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        hourly_file.write_text("")  # empty
        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=daily_file,
            db_path=db,
        )
        row = await agg.build_daily(date_iso="2026-04-17")
        assert row["hourly_missing"] is True
        assert row["cycles_total"] == 0


class TestRetention:
    @pytest.mark.asyncio
    async def test_trim_removes_events_older_than_cutoff(
        self, events_file, tmp_path,
    ):
        _write_events(events_file, [
            {"ts": 100.0, "type": "gold_delta", "amount": 1},
            {"ts": 200.0, "type": "gold_delta", "amount": 2},
            {"ts": 300.0, "type": "gold_delta", "amount": 3},
        ])
        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=tmp_path / "h.jsonl",
            daily_file=tmp_path / "d.jsonl",
            db_path=tmp_path / "t.db",
        )
        removed = await agg.trim_events(cutoff_ts=250.0)
        assert removed == 2
        remaining = [
            json.loads(l) for l in events_file.read_text().splitlines() if l.strip()
        ]
        assert len(remaining) == 1
        assert remaining[0]["ts"] == 300.0

    @pytest.mark.asyncio
    async def test_trim_noop_when_nothing_to_remove(self, events_file, tmp_path):
        _write_events(events_file, [{"ts": 500.0, "type": "x"}])
        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=tmp_path / "h.jsonl",
            daily_file=tmp_path / "d.jsonl",
            db_path=tmp_path / "t.db",
        )
        removed = await agg.trim_events(cutoff_ts=100.0)
        assert removed == 0
