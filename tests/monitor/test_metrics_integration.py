"""End-to-end test of the metrics pipeline with a fresh tmp directory."""
from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from anima.core.bus import EventBus
from anima.monitor.metrics_pipeline import record
from anima.monitor.metrics_pipeline.aggregator import MetricsAggregator
from anima.monitor.metrics_pipeline.alerts import MetricsAlertDetector


@pytest.mark.asyncio
async def test_end_to_end(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    hourly = tmp_path / "hourly.jsonl"
    daily = tmp_path / "daily.jsonl"
    alerts = tmp_path / "alerts.jsonl"
    db = tmp_path / "anima.db"

    async with aiosqlite.connect(db) as conn:
        await conn.execute("""
            CREATE TABLE action_logs (
                timestamp REAL, agent TEXT, procedure TEXT,
                location_x INTEGER, location_y INTEGER,
                result TEXT, message TEXT, duration_ms REAL, details TEXT
            )
        """)
        await conn.executemany(
            "INSERT INTO action_logs VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (100.0, "G", "mine_ore", 0, 0, "success", "", 3500.0, "{}"),
                (200.0, "G", "mine_ore", 0, 0, "blocked", "", 2000.0, "{}"),
                (300.0, "G", "smelt_ore", 0, 0, "success", "", 2000.0, "{}"),
            ],
        )
        await conn.commit()

    # Emit a handful of events spanning one hour
    for type_, kw in [
        ("gold_delta", {"amount": 50}),
        ("gold_delta", {"amount": -10}),
        ("cycle_complete", {"cycles_completed": 1, "duration_s": 180.0}),
        ("death", {"pos": [0, 0], "hp_before": 15}),
        ("stuck_event", {"reason": "idle"}),
    ]:
        record(type_, events_file=events, **kw)

    # Set event timestamps to fall into a window [100, 3700]
    lines = events.read_text().splitlines()
    base = 100.0
    aligned = []
    for i, line in enumerate(lines):
        obj = json.loads(line)
        obj["ts"] = base + i * 10
        aligned.append(json.dumps(obj))
    events.write_text("\n".join(aligned) + "\n")

    agg = MetricsAggregator(
        events_file=events, hourly_file=hourly, daily_file=daily, db_path=db,
    )
    hourly_row = await agg.build_hourly(window_start=0.0, window_end=3600.0)

    # Sanity
    assert hourly_row["cycles_completed"] == 1
    assert hourly_row["deaths"] == 1
    assert hourly_row["gold"]["earned"] == 50
    assert hourly_row["gold"]["spent"] == 10
    assert hourly_row["procedures"]["mine_ore"]["ok"] == 1
    assert hourly_row["procedures"]["mine_ore"]["fail"] == 1

    # Alerts — should trigger on the one death
    det = MetricsAlertDetector(alerts_file=alerts)
    fired = det.check(hourly_row)
    assert any(a["rule"] == "death" for a in fired)
    assert alerts.exists()


@pytest.mark.asyncio
async def test_alert_fires_through_bus_wrapper(tmp_path: Path):
    """Regression test: alert detector must unwrap bus payload (not see the
    {'message', 'row', ...} wrapper and evaluate rules against it)."""
    events = tmp_path / "events.jsonl"
    hourly = tmp_path / "hourly.jsonl"
    daily = tmp_path / "daily.jsonl"
    alerts = tmp_path / "alerts.jsonl"
    db = tmp_path / "anima.db"

    async with aiosqlite.connect(db) as conn:
        await conn.execute("""
            CREATE TABLE action_logs (
                timestamp REAL, agent TEXT, procedure TEXT,
                location_x INTEGER, location_y INTEGER,
                result TEXT, message TEXT, duration_ms REAL, details TEXT
            )
        """)
        await conn.commit()

    bus = EventBus()
    agg = MetricsAggregator(
        events_file=events, hourly_file=hourly, daily_file=daily,
        db_path=db, bus=bus,
    )
    det = MetricsAlertDetector(alerts_file=alerts, bus=bus)

    # Wire the subscription exactly like avatar.py does
    bus.subscribe(
        "metrics.hourly_complete",
        lambda _t, data: det.check(data.get("row", data)),
    )

    # Emit a death event into the events file, then align its timestamp to
    # fall inside the aggregation window [0, 3600).
    record("death", events_file=events, pos=[0, 0], hp_before=10)
    raw = json.loads(events.read_text())
    raw["ts"] = 100.0
    events.write_text(json.dumps(raw) + "\n")

    # Aggregator publishes via bus → subscriber unwraps → detector fires
    await agg.build_hourly(window_start=0.0, window_end=3600.0)

    # The alert detector should have persisted a death alert
    lines = alerts.read_text().splitlines() if alerts.exists() else []
    assert any(json.loads(line)["rule"] == "death" for line in lines), \
        f"death alert should fire through bus wrapper; got alerts file: {lines!r}"
