"""Tests for MetricsAlertDetector."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anima.monitor.metrics_pipeline.alerts import MetricsAlertDetector


@pytest.fixture
def alerts_file(tmp_path: Path) -> Path:
    return tmp_path / "metrics_alerts.jsonl"


def _hourly_row(cycles=2, deaths=0, stuck_events=0, procs_ok=10, procs_fail=1,
                hour="2026-04-17T12:00:00+00:00"):
    return {
        "hour": hour,
        "uptime_s": 3600,
        "procedures": {
            "mine_ore": {"ok": procs_ok, "fail": procs_fail, "avg_ms": 3500},
        },
        "cycles_completed": cycles,
        "phase_transitions": {},
        "gold": {"earned": 50, "spent": 10, "delta": 40},
        "deaths": deaths,
        "stuck_events": stuck_events,
        "skills": {},
    }


class TestAlertRules:
    def test_single_death_fires_alert(self, alerts_file):
        det = MetricsAlertDetector(alerts_file=alerts_file)
        alerts = det.check(_hourly_row(deaths=1))
        assert any(a["rule"] == "death" for a in alerts)

    def test_stuck_events_over_5_fires(self, alerts_file):
        det = MetricsAlertDetector(alerts_file=alerts_file)
        alerts = det.check(_hourly_row(stuck_events=6))
        assert any(a["rule"] == "stuck_events" for a in alerts)

    def test_stuck_events_at_5_does_not_fire(self, alerts_file):
        det = MetricsAlertDetector(alerts_file=alerts_file)
        alerts = det.check(_hourly_row(stuck_events=5))
        assert all(a["rule"] != "stuck_events" for a in alerts)

    def test_cycles_regression_requires_3_consecutive(self, alerts_file):
        det = MetricsAlertDetector(alerts_file=alerts_file)
        # Establish a baseline of 4 cycles/hour over 6 hours
        for _ in range(6):
            det.check(_hourly_row(cycles=4))
        # Two low hours — should NOT fire yet
        a1 = det.check(_hourly_row(cycles=1))
        a2 = det.check(_hourly_row(cycles=1))
        assert all(a["rule"] != "cycles_regression" for a in (a1 + a2))
        # Third low hour — should fire
        a3 = det.check(_hourly_row(cycles=1))
        assert any(a["rule"] == "cycles_regression" for a in a3)

    def test_cycles_recovery_clears_streak(self, alerts_file):
        det = MetricsAlertDetector(alerts_file=alerts_file)
        for _ in range(6):
            det.check(_hourly_row(cycles=4))
        det.check(_hourly_row(cycles=1))
        det.check(_hourly_row(cycles=1))
        # Recovery hour
        det.check(_hourly_row(cycles=4))
        # Next low hour is only streak-of-1 again
        alerts = det.check(_hourly_row(cycles=1))
        assert all(a["rule"] != "cycles_regression" for a in alerts)

    def test_procedure_success_below_60pct_for_2h(self, alerts_file):
        det = MetricsAlertDetector(alerts_file=alerts_file)
        # First low-success hour
        a1 = det.check(_hourly_row(procs_ok=3, procs_fail=7))  # 30%
        assert all(a["rule"] != "low_success_rate" for a in a1)
        # Second consecutive low hour
        a2 = det.check(_hourly_row(procs_ok=5, procs_fail=5))  # 50%
        assert any(a["rule"] == "low_success_rate" for a in a2)

    def test_alerts_persisted_to_file(self, alerts_file):
        det = MetricsAlertDetector(alerts_file=alerts_file)
        det.check(_hourly_row(deaths=1))
        lines = alerts_file.read_text().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["rule"] == "death"
