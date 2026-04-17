"""Tests for MetricsCollector."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from anima.monitor.metrics_pipeline.collector import MetricsCollector


@pytest.fixture
def events_file(tmp_path: Path) -> Path:
    return tmp_path / "metrics_events.jsonl"


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines()]


def _make_state(**status_overrides) -> dict:
    default_status = {
        "hp": 112, "hp_max": 112,
        "gold": 50, "weight": 100, "weight_max": 474,
        "x": 2553, "y": 496,
    }
    default_status.update(status_overrides)
    return {
        "ts": 0.0,
        "status": default_status,
        "inventory": [],
        "skills": {"list": []},
    }


class TestStateDiff:
    def test_gold_delta_emitted_on_increase(self, events_file: Path):
        col = MetricsCollector(events_file=events_file)
        col._diff_state(_make_state(gold=50), _make_state(gold=75))
        events = _read(events_file)
        assert len(events) == 1
        assert events[0]["type"] == "gold_delta"
        assert events[0]["amount"] == 25

    def test_gold_delta_emitted_on_decrease(self, events_file: Path):
        col = MetricsCollector(events_file=events_file)
        col._diff_state(_make_state(gold=100), _make_state(gold=70))
        events = _read(events_file)
        assert events[0]["amount"] == -30

    def test_no_gold_delta_when_unchanged(self, events_file: Path):
        col = MetricsCollector(events_file=events_file)
        col._diff_state(_make_state(gold=50), _make_state(gold=50))
        assert _read(events_file) == []

    def test_death_on_hp_zero_transition(self, events_file: Path):
        col = MetricsCollector(events_file=events_file)
        col._diff_state(
            _make_state(hp=20, x=2553, y=496),
            _make_state(hp=0, x=2553, y=496),
        )
        events = _read(events_file)
        types = [e["type"] for e in events]
        assert "death" in types
        death = next(e for e in events if e["type"] == "death")
        assert death["pos"] == [2553, 496]
        assert death["hp_before"] == 20

    def test_no_duplicate_death_while_dead(self, events_file: Path):
        """Staying dead at hp=0 must not fire a second death event."""
        col = MetricsCollector(events_file=events_file)
        col._diff_state(_make_state(hp=20), _make_state(hp=0))
        col._diff_state(_make_state(hp=0), _make_state(hp=0))
        events = _read(events_file)
        death_events = [e for e in events if e["type"] == "death"]
        assert len(death_events) == 1

    def test_skill_delta(self, events_file: Path):
        col = MetricsCollector(events_file=events_file)
        before = _make_state()
        before["skills"] = {"list": [{"id": 7, "value": 63.9}]}
        after = _make_state()
        after["skills"] = {"list": [{"id": 7, "value": 64.0}]}
        col._diff_state(before, after)
        events = _read(events_file)
        skill_events = [e for e in events if e["type"] == "skill_delta"]
        assert len(skill_events) == 1
        assert skill_events[0]["skill_id"] == 7
        assert skill_events[0]["from"] == 63.9
        assert skill_events[0]["to"] == 64.0

    def test_first_snapshot_emits_nothing(self, events_file: Path):
        """With no previous snapshot, no diff events should fire."""
        col = MetricsCollector(events_file=events_file)
        col._diff_state(None, _make_state(gold=50))
        assert _read(events_file) == []
