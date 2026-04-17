"""Tests for tools/metrics.py CLI."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
CLI = ROOT / "tools" / "metrics.py"


def _run(args: list[str], env_overrides: dict) -> subprocess.CompletedProcess:
    import os
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(CLI)] + args,
        capture_output=True, text=True, env=env,
    )


def test_today_with_no_data_prints_placeholder(tmp_path: Path):
    env = {
        "ANIMA_METRICS_HOURLY": str(tmp_path / "h.jsonl"),
        "ANIMA_METRICS_DAILY": str(tmp_path / "d.jsonl"),
        "ANIMA_METRICS_EVENTS": str(tmp_path / "e.jsonl"),
    }
    result = _run(["--today"], env)
    assert result.returncode == 0
    assert "no data" in result.stdout.lower() or "cycles" in result.stdout.lower()


def test_today_json_mode(tmp_path: Path):
    hourly = tmp_path / "h.jsonl"
    import time as _t
    today_prefix = _t.strftime("%Y-%m-%d", _t.gmtime())
    hourly.write_text(json.dumps({
        "hour": f"{today_prefix}T00:00:00+00:00",
        "uptime_s": 3600,
        "procedures": {"mine_ore": {"ok": 10, "fail": 2, "avg_ms": 3500}},
        "cycles_completed": 2,
        "phase_transitions": {},
        "gold": {"earned": 50, "spent": 10, "delta": 40},
        "deaths": 0, "stuck_events": 1, "skills": {},
    }) + "\n")

    env = {
        "ANIMA_METRICS_HOURLY": str(hourly),
        "ANIMA_METRICS_DAILY": str(tmp_path / "d.jsonl"),
        "ANIMA_METRICS_EVENTS": str(tmp_path / "e.jsonl"),
    }
    result = _run(["--today", "--json"], env)
    assert result.returncode == 0
    data = json.loads(result.stdout.strip() or "[]")
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["cycles_completed"] == 2


def test_top_failures(tmp_path: Path):
    hourly = tmp_path / "h.jsonl"
    import time as _t
    today_prefix = _t.strftime("%Y-%m-%d", _t.gmtime())
    hourly.write_text(json.dumps({
        "hour": f"{today_prefix}T00:00:00+00:00",
        "uptime_s": 3600,
        "procedures": {
            "mine_ore": {"ok": 3, "fail": 10, "avg_ms": 3500},
            "smelt_ore": {"ok": 5, "fail": 2, "avg_ms": 2000},
        },
        "cycles_completed": 0, "phase_transitions": {},
        "gold": {"earned": 0, "spent": 0, "delta": 0},
        "deaths": 0, "stuck_events": 0, "skills": {},
    }) + "\n")
    env = {
        "ANIMA_METRICS_HOURLY": str(hourly),
        "ANIMA_METRICS_DAILY": str(tmp_path / "d.jsonl"),
        "ANIMA_METRICS_EVENTS": str(tmp_path / "e.jsonl"),
    }
    result = _run(["--top-failures", "24h"], env)
    assert result.returncode == 0
    assert "mine_ore" in result.stdout
    # mine_ore failures should be listed before smelt_ore
    assert result.stdout.find("mine_ore") < result.stdout.find("smelt_ore")
