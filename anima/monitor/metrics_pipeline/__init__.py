"""Metrics pipeline — event stream + hourly/daily rollups + alerts.

See docs/superpowers/specs/2026-04-17-metrics-pipeline-design.md.

Three concerns:
  - collector.py: MetricsCollector (state-poll + bus + action_logs poll)
  - aggregator.py: MetricsAggregator (hourly + daily rollups + retention)
  - alerts.py: MetricsAlertDetector (threshold rules)

Module-level record() is the manual-emit escape hatch for callers that
cannot be reached by the automatic pipelines.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

ROOT = Path(__file__).parent.parent.parent.parent
DEFAULT_EVENTS_FILE = ROOT / "data" / "metrics_events.jsonl"


def record(
    event_type: str,
    *,
    events_file: Path | None = None,
    **payload: Any,
) -> None:
    """Append one event to the raw event stream.

    Safe for use from any loop — never raises. Uses append-mode write
    which is OS-atomic for payloads under PIPE_BUF (typically 4 KB).
    """
    target = events_file or DEFAULT_EVENTS_FILE
    row = {"ts": time.time(), "type": event_type, **payload}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("metrics_record_failed", event_type=event_type, error=str(e))


__all__ = ["record", "DEFAULT_EVENTS_FILE"]
