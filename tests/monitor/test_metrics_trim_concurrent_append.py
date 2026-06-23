"""Regression: trim_events must not clobber a concurrent record() append.

``trim_events`` snapshots the events file, drops expired rows, and atomically
replaces the whole file. ``record()`` (documented "Safe for use from any loop")
appends new events to the same file concurrently. A naive snapshot-then-replace
would overwrite the file with the stale snapshot, silently DESTROYING every
event appended during the trim window. This test forces an append to land
inside that window and asserts the new event survives.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anima.monitor.metrics_pipeline import record
from anima.monitor.metrics_pipeline.aggregator import (
    MetricsAggregator,
    _read_events_in_window,
)


def _make_agg(events_file: Path, tmp_path: Path) -> MetricsAggregator:
    return MetricsAggregator(
        events_file=events_file,
        hourly_file=tmp_path / "h.jsonl",
        daily_file=tmp_path / "d.jsonl",
        db_path=tmp_path / "t.db",
    )


@pytest.mark.asyncio
async def test_trim_preserves_concurrent_append(monkeypatch, tmp_path):
    events_file = tmp_path / "metrics_events.jsonl"
    # Three old rows (ts < cutoff) that the trim will drop, one fresh row kept.
    events_file.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"ts": 10.0, "type": "gold_delta", "amount": 1},
                {"ts": 20.0, "type": "gold_delta", "amount": 2},
                {"ts": 5000.0, "type": "gold_delta", "amount": 3},
            ]
        )
        + "\n"
    )

    agg = _make_agg(events_file, tmp_path)

    # Simulate a concurrent producer: exactly once, while trim is mid-filter
    # (after it has snapshotted the file but before it replaces it), append a
    # brand-new event via the real record() append path. Hooking json.loads —
    # which the filter loop calls per line — lands the append squarely inside
    # the read→replace window.
    fired = {"done": False}
    real_loads = json.loads

    def _loads_with_concurrent_append(s, *args, **kwargs):
        if not fired["done"]:
            fired["done"] = True
            record("gold_delta", events_file=events_file, ts=9999.0, amount=999)
        return real_loads(s, *args, **kwargs)

    monkeypatch.setattr(
        "anima.monitor.metrics_pipeline.aggregator.json.loads",
        _loads_with_concurrent_append,
    )

    removed = await agg.trim_events(cutoff_ts=100.0)

    assert fired["done"], "test did not actually inject a concurrent append"
    assert removed == 2  # the two ts<100 rows

    # The concurrently-appended event (ts=9999.0, amount=999) MUST survive the
    # atomic replace, alongside the row the filter kept (ts=5000.0).
    rows = _read_events_in_window(events_file, 0.0, 1e12)
    amounts = sorted(r["amount"] for r in rows)
    assert amounts == [3, 999], (
        "concurrent record() append was clobbered by trim_events replace"
    )
