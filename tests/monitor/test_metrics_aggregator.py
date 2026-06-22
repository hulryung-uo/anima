"""Tests for MetricsAggregator."""
from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from anima.monitor.metrics_pipeline.aggregator import MetricsAggregator
from anima.monitor.metrics_pipeline.aggregator import _aggregate_skill_deltas


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
    async def test_typeless_event_does_not_crash_rollup(
        self, events_file, hourly_file, tmp_path,
    ):
        # A valid-JSON event line that lacks a "type" key (older schema, a
        # partially-flushed concurrent write, or another producer) lands in the
        # window: _read_events_in_window keeps it (it only filters on ts), so
        # build_hourly must tolerate it. The old `e["type"]` accesses raised
        # KeyError here, aborting run_once() — and because next_start only
        # advances after a SUCCESSFUL build_hourly, the whole hourly/daily
        # rollup (and the 30-day trim) stayed permanently wedged at that hour,
        # retrying the same crash every 60s. A typeless event must simply be
        # ignored, like the read path already ignores unparseable lines.
        db = tmp_path / "t.db"
        await _make_db(db)
        _write_events(events_file, [
            {"ts": 100.0, "type": "gold_delta", "amount": 50},
            {"ts": 120.0, "foo": "bar"},  # valid JSON, no "type"
            {"ts": 150.0, "type": "death", "pos": [0, 0]},
            {"ts": 160.0, "skill_id": 45, "from": 1.0, "to": 2.0},  # no "type"
        ])

        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=tmp_path / "d.jsonl",
            db_path=db,
        )
        # Must not raise; the typeless events are ignored, the well-formed
        # ones still aggregate.
        row = await agg.build_hourly(window_start=0.0, window_end=3600.0)
        assert row["gold"]["earned"] == 50
        assert row["deaths"] == 1
        # The typeless skill-shaped event contributes no skill delta.
        assert row["skills"] == {}

    @pytest.mark.asyncio
    async def test_non_dict_event_line_does_not_crash_rollup(
        self, events_file, hourly_file, tmp_path,
    ):
        # A torn concurrent append (or a stray write from another producer) can
        # leave a line that is VALID JSON but not an object — a bare number,
        # string, list, or null. _read_events_in_window's `obj.get("ts", 0)`
        # then raised AttributeError, aborting build_hourly. Because run_once()
        # only advances next_start after a SUCCESSFUL build_hourly, that one
        # line wedged the entire hourly/daily/trim pipeline forever, retrying
        # the same crash every 60s. The reader must skip a non-object line the
        # same way it skips an unparseable one.
        db = tmp_path / "t.db"
        await _make_db(db)
        # Write raw lines directly (not via _write_events, which json-dumps
        # dicts) so the file contains genuine non-object JSON lines.
        events_file.write_text(
            "\n".join([
                json.dumps({"ts": 100.0, "type": "gold_delta", "amount": 50}),
                "42",            # bare number (e.g. a torn append)
                '"orphan"',      # bare string
                "[1, 2, 3]",     # bare list
                "null",          # bare null
                json.dumps({"ts": 150.0, "type": "death", "pos": [0, 0]}),
            ]) + "\n"
        )

        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=tmp_path / "d.jsonl",
            db_path=db,
        )
        # Must not raise; the non-object lines are ignored, the well-formed
        # events still aggregate.
        row = await agg.build_hourly(window_start=0.0, window_end=3600.0)
        assert row["gold"]["earned"] == 50
        assert row["deaths"] == 1

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
        rows = [json.loads(line) for line in lines]
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

    @pytest.mark.asyncio
    async def test_no_procedure_runs_reports_no_data_not_zero(
        self, events_file, hourly_file, daily_file, tmp_path,
    ):
        # A day whose hourly rows exist but contain ZERO procedure runs (agent
        # was up but only did non-procedure activity, or action_logs was empty)
        # must report procedure_success_rate as the "no data" sentinel 1.0 —
        # the same empty-denominator convention build_hourly's summary and
        # alerts._success_rate already use. Defaulting to 0.0 instead painted a
        # no-data day as a catastrophic 0% (total failure) on the dashboard and
        # in tools/metrics.py's consumer.
        db = tmp_path / "t.db"
        await _make_db(db)
        hourly_rows = [
            {
                "hour": "2026-04-18T00:00:00+00:00",
                "uptime_s": 3600,
                "procedures": {},  # no procedure runs at all
                "cycles_completed": 0,
                "phase_transitions": {},
                "gold": {"earned": 0, "spent": 0, "delta": 0},
                "deaths": 0,
                "stuck_events": 0,
                "skills": {},
            },
            {
                "hour": "2026-04-18T01:00:00+00:00",
                "uptime_s": 3600,
                "procedures": {},  # still no procedure runs
                "cycles_completed": 0,
                "phase_transitions": {},
                "gold": {"earned": 0, "spent": 0, "delta": 0},
                "deaths": 0,
                "stuck_events": 0,
                "skills": {},
            },
        ]
        hourly_file.write_text(
            "\n".join(json.dumps(r) for r in hourly_rows) + "\n"
        )
        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=daily_file,
            db_path=db,
        )
        row = await agg.build_daily(date_iso="2026-04-18")
        # hourly rows DO exist, so this is not a "missing hourly" case...
        assert row["hourly_missing"] is False
        # ...but with no procedure runs the rate is "no data" (1.0), not 0%.
        assert row["procedure_success_rate"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_real_failures_still_lower_success_rate(
        self, events_file, hourly_file, daily_file, tmp_path,
    ):
        # Guard the fix against over-correction: a day that DOES have runs and
        # some failures must still report the true ratio, not 1.0.
        db = tmp_path / "t.db"
        await _make_db(db)
        hourly_rows = [
            {
                "hour": "2026-04-19T00:00:00+00:00",
                "uptime_s": 3600,
                "procedures": {"mine_ore": {"ok": 1, "fail": 3, "avg_ms": 0}},
                "cycles_completed": 0,
                "phase_transitions": {},
                "gold": {"earned": 0, "spent": 0, "delta": 0},
                "deaths": 0,
                "stuck_events": 0,
                "skills": {},
            },
        ]
        hourly_file.write_text(
            "\n".join(json.dumps(r) for r in hourly_rows) + "\n"
        )
        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=daily_file,
            db_path=db,
        )
        row = await agg.build_daily(date_iso="2026-04-19")
        # 1 ok / 4 runs = 0.25, unaffected by the empty-denominator default.
        assert row["procedure_success_rate"] == pytest.approx(0.25)


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
            json.loads(line) for line in events_file.read_text().splitlines() if line.strip()
        ]
        assert len(remaining) == 1
        assert remaining[0]["ts"] == 300.0

    @pytest.mark.asyncio
    async def test_trim_keeps_non_dict_line_without_crashing(
        self, events_file, tmp_path,
    ):
        # trim_events is exactly the path that would PURGE a wedging line, so
        # it must survive a non-object JSON line (bare number/string/list/null)
        # rather than crash on `obj.get("ts", 0)`. It keeps such a line verbatim
        # (it has no comparable ts), like an unparseable line, while still
        # trimming the real expired dict events around it.
        events_file.write_text(
            "\n".join([
                json.dumps({"ts": 100.0, "type": "gold_delta", "amount": 1}),
                "42",            # non-object line: no ts to compare
                json.dumps({"ts": 300.0, "type": "gold_delta", "amount": 3}),
            ]) + "\n"
        )
        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=tmp_path / "h.jsonl",
            daily_file=tmp_path / "d.jsonl",
            db_path=tmp_path / "t.db",
        )
        removed = await agg.trim_events(cutoff_ts=250.0)
        assert removed == 1  # only the ts=100 dict is expired
        remaining = [
            line for line in events_file.read_text().splitlines() if line.strip()
        ]
        # The non-object line is preserved verbatim alongside the kept dict.
        assert "42" in remaining
        assert any('"ts": 300.0' in r for r in remaining)
        assert len(remaining) == 2

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


class TestTickLoop:
    @pytest.mark.asyncio
    async def test_run_once_builds_hourly_for_previous_hour(
        self, events_file, hourly_file, daily_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        events_file.write_text("")

        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=daily_file,
            db_path=db,
        )
        # Pretend the current time is exactly at some hour boundary + 5s
        now = 1_700_000_000.0  # well into the future
        # Align to hour
        aligned = now - (now % 3600)
        await agg.run_once(now=aligned + 5.0)
        # Should have produced one hourly row for [aligned-3600, aligned]
        lines = [line for line in hourly_file.read_text().splitlines() if line.strip()]
        assert len(lines) == 1
        row = json.loads(lines[0])
        # Hour iso should be one hour before `aligned`
        import datetime as _dt
        expected_hour = _dt.datetime.fromtimestamp(
            aligned - 3600, tz=_dt.timezone.utc
        ).isoformat()
        assert row["hour"].startswith(expected_hour[:13])

    @pytest.mark.asyncio
    async def test_run_once_noop_within_same_hour(
        self, events_file, hourly_file, daily_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        events_file.write_text("")
        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=daily_file,
            db_path=db,
        )
        now = 1_700_000_005.0
        await agg.run_once(now=now)
        await agg.run_once(now=now + 30)
        # Should only produce one hourly row even with two ticks in the same hour
        lines = [line for line in hourly_file.read_text().splitlines() if line.strip()]
        assert len(lines) == 1

    @pytest.mark.asyncio
    async def test_backfill_missing_hours(
        self, events_file, hourly_file, daily_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        events_file.write_text("")
        # Seed one old hourly row, three hours ago
        base = 1_700_000_000.0 - (1_700_000_000.0 % 3600)
        three_hours_ago_iso = "2023-11-14T19:00:00+00:00"  # arbitrary past hour
        hourly_file.write_text(
            json.dumps({
                "hour": three_hours_ago_iso,
                "uptime_s": 3600, "procedures": {},
                "cycles_completed": 0, "phase_transitions": {},
                "gold": {"earned": 0, "spent": 0, "delta": 0},
                "deaths": 0, "stuck_events": 0, "skills": {},
            }) + "\n"
        )

        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=daily_file,
            db_path=db,
        )
        now = base + 5.0
        # Should backfill at most 3 hours plus the current boundary hour
        n = await agg.run_once(now=now)
        assert n >= 1


class TestSkillDeltaAggregation:
    """_aggregate_skill_deltas must pick the earliest 'from' and latest 'to'
    by timestamp, not by position in the (possibly out-of-order) event list."""

    def _ev(self, sid: int, ts: float, frm: float, to: float) -> dict:
        return {"type": "skill_delta", "skill_id": sid, "ts": ts,
                "from": frm, "to": to}

    def test_in_order_events_give_full_span(self):
        out = _aggregate_skill_deltas([
            self._ev(45, 100.0, 50.0, 50.2),
            self._ev(45, 200.0, 50.2, 50.5),
            self._ev(45, 300.0, 50.5, 50.9),
        ])
        assert out["45"] == {"from": 50.0, "to": 50.9}

    def test_out_of_order_events_still_pick_latest_to(self):
        # The skill genuinely went 50.0 -> 50.9, but the +0.4 step (latest by
        # ts) is appended to the stream *before* an earlier step. The old code
        # set 'to' in list order and would report to=50.5, undercounting the
        # gain by 0.4. Guarding 'to' by timestamp fixes it.
        out = _aggregate_skill_deltas([
            self._ev(45, 100.0, 50.0, 50.2),
            self._ev(45, 300.0, 50.5, 50.9),   # latest by ts, earlier in list
            self._ev(45, 200.0, 50.2, 50.5),   # earlier by ts, later in list
        ])
        assert out["45"] == {"from": 50.0, "to": 50.9}
        # And the resulting delta is the true before->after span.
        assert out["45"]["to"] - out["45"]["from"] == pytest.approx(0.9)

    def test_out_of_order_events_still_pick_earliest_from(self):
        # The true earliest sample (ts=100) arrives last in the list. The old
        # code overwrote _earliest_ts unconditionally, so a later-ts event
        # processed first would block the real earliest 'from' from winning.
        out = _aggregate_skill_deltas([
            self._ev(7, 300.0, 60.5, 60.9),
            self._ev(7, 200.0, 60.2, 60.5),
            self._ev(7, 100.0, 60.0, 60.2),   # earliest by ts, last in list
        ])
        assert out["7"] == {"from": 60.0, "to": 60.9}

    @pytest.mark.asyncio
    async def test_hourly_skill_gain_uses_full_span(
        self, events_file, hourly_file, tmp_path,
    ):
        db = tmp_path / "t.db"
        await _make_db(db)
        # Mining (id 45) trains 50.0 -> 50.9, but events are written
        # out of timestamp order in the raw stream.
        _write_events(events_file, [
            {"type": "skill_delta", "skill_id": 45, "ts": 100.0,
             "from": 50.0, "to": 50.2},
            {"type": "skill_delta", "skill_id": 45, "ts": 300.0,
             "from": 50.5, "to": 50.9},
            {"type": "skill_delta", "skill_id": 45, "ts": 200.0,
             "from": 50.2, "to": 50.5},
        ])
        agg = MetricsAggregator(
            events_file=events_file,
            hourly_file=hourly_file,
            daily_file=tmp_path / "d.jsonl",
            db_path=db,
        )
        row = await agg.build_hourly(window_start=0.0, window_end=3600.0)
        assert row["skills"]["45"]["from"] == pytest.approx(50.0)
        assert row["skills"]["45"]["to"] == pytest.approx(50.9)
