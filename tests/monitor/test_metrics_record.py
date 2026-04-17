"""Tests for anima.monitor.metrics_pipeline.record()."""
from __future__ import annotations

import json
from pathlib import Path

from anima.monitor.metrics_pipeline import record


def test_record_appends_single_line(tmp_path: Path):
    events_file = tmp_path / "metrics_events.jsonl"
    record("procedure_end", events_file=events_file, proc="mine_ore", success=True)
    lines = events_file.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["type"] == "procedure_end"
    assert entry["proc"] == "mine_ore"
    assert entry["success"] is True
    assert isinstance(entry["ts"], float)


def test_record_appends_multiple_events(tmp_path: Path):
    events_file = tmp_path / "metrics_events.jsonl"
    record("gold_delta", events_file=events_file, amount=10, reason="sell")
    record("gold_delta", events_file=events_file, amount=-5, reason="buy")
    record("death", events_file=events_file, pos=[2553, 496], hp_before=12)
    lines = events_file.read_text().splitlines()
    assert len(lines) == 3
    types = [json.loads(l)["type"] for l in lines]
    assert types == ["gold_delta", "gold_delta", "death"]


def test_record_creates_parent_directory(tmp_path: Path):
    events_file = tmp_path / "nested" / "dir" / "metrics_events.jsonl"
    record("test_event", events_file=events_file, foo="bar")
    assert events_file.exists()
    entry = json.loads(events_file.read_text().strip())
    assert entry["foo"] == "bar"


def test_record_timestamp_monotonic(tmp_path: Path):
    events_file = tmp_path / "metrics_events.jsonl"
    record("a", events_file=events_file)
    record("b", events_file=events_file)
    lines = events_file.read_text().splitlines()
    ts1 = json.loads(lines[0])["ts"]
    ts2 = json.loads(lines[1])["ts"]
    assert ts2 >= ts1  # at least non-decreasing


def test_record_ignores_write_errors(tmp_path: Path):
    """record() must never raise even when the file cannot be opened."""
    events_file = tmp_path / "will_fail"
    events_file.mkdir()  # a directory with this name — open('w+') will fail
    # Must not raise
    record("test", events_file=events_file, value=1)
