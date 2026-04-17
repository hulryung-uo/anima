"""MetricsAlertDetector — threshold rules over hourly rollup rows."""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

import structlog

logger = structlog.get_logger()


class MetricsAlertDetector:
    """Evaluates each new hourly row against a small set of rules.

    Rules:
      - death: any hour with deaths > 0
      - stuck_events: any hour with stuck_events > 5
      - cycles_regression: 3 consecutive hours with cycles <= 0.5 * prior-6h-mean
      - low_success_rate: 2 consecutive hours with proc success rate < 60%
    """

    STUCK_THRESHOLD = 5
    REGRESSION_MULT = 0.5
    REGRESSION_BASELINE_HOURS = 6
    REGRESSION_CONSECUTIVE_HOURS = 3
    SUCCESS_RATE_THRESHOLD = 0.60
    SUCCESS_RATE_CONSECUTIVE_HOURS = 2

    def __init__(self, *, alerts_file: Path, bus=None) -> None:
        self.alerts_file = alerts_file
        self.bus = bus
        self._cycles_history: deque[int] = deque(
            maxlen=self.REGRESSION_BASELINE_HOURS + self.REGRESSION_CONSECUTIVE_HOURS
        )
        self._success_history: deque[float] = deque(maxlen=4)

    def check(self, hourly_row: dict) -> list[dict]:
        fired: list[dict] = []

        # Rule: death
        if hourly_row.get("deaths", 0) > 0:
            fired.append({
                "rule": "death",
                "value": hourly_row["deaths"],
                "message": f"deaths={hourly_row['deaths']} in hour {hourly_row.get('hour', '?')}",
            })

        # Rule: stuck_events
        stuck = hourly_row.get("stuck_events", 0)
        if stuck > self.STUCK_THRESHOLD:
            fired.append({
                "rule": "stuck_events",
                "value": stuck,
                "message": f"stuck_events={stuck} exceeds {self.STUCK_THRESHOLD}/h",
            })

        # Rule: cycles_regression
        self._cycles_history.append(hourly_row.get("cycles_completed", 0))
        reg = self._check_cycles_regression()
        if reg is not None:
            fired.append(reg)

        # Rule: low_success_rate
        rate = _success_rate(hourly_row)
        self._success_history.append(rate)
        sr = self._check_success_rate()
        if sr is not None:
            fired.append(sr)

        # Persist + optionally publish
        for alert in fired:
            alert_ts = time.time()
            line = json.dumps({"ts": alert_ts, **alert}, ensure_ascii=False)
            try:
                self.alerts_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.alerts_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as e:
                logger.warning("metrics_alert_persist_failed", error=str(e))
            if self.bus is not None:
                try:
                    self.bus.publish("metrics.alert", alert)
                except Exception as e:
                    logger.warning("metrics_alert_publish_failed", error=str(e))

        return fired

    def _check_cycles_regression(self) -> dict | None:
        hist = list(self._cycles_history)
        tail = hist[-self.REGRESSION_CONSECUTIVE_HOURS:]
        if len(tail) < self.REGRESSION_CONSECUTIVE_HOURS:
            return None
        baseline_slice = hist[
            -(self.REGRESSION_CONSECUTIVE_HOURS + self.REGRESSION_BASELINE_HOURS)
            : -self.REGRESSION_CONSECUTIVE_HOURS
        ]
        if not baseline_slice:
            return None
        baseline = sum(baseline_slice) / len(baseline_slice)
        threshold = self.REGRESSION_MULT * baseline
        if baseline > 0 and all(v <= threshold for v in tail):
            return {
                "rule": "cycles_regression",
                "baseline": round(baseline, 2),
                "recent": tail,
                "message": (
                    f"cycles dropped to {tail} (baseline {baseline:.1f}/h)"
                ),
            }
        return None

    def _check_success_rate(self) -> dict | None:
        tail = list(self._success_history)[-self.SUCCESS_RATE_CONSECUTIVE_HOURS:]
        if len(tail) < self.SUCCESS_RATE_CONSECUTIVE_HOURS:
            return None
        if all(r < self.SUCCESS_RATE_THRESHOLD for r in tail):
            return {
                "rule": "low_success_rate",
                "recent": [round(r, 3) for r in tail],
                "message": (
                    f"procedure success rate below "
                    f"{self.SUCCESS_RATE_THRESHOLD:.0%} for "
                    f"{self.SUCCESS_RATE_CONSECUTIVE_HOURS}h"
                ),
            }
        return None


def _success_rate(hourly_row: dict) -> float:
    total_ok = 0
    total_fail = 0
    for stats in hourly_row.get("procedures", {}).values():
        total_ok += stats.get("ok", 0)
        total_fail += stats.get("fail", 0)
    total = total_ok + total_fail
    return (total_ok / total) if total else 1.0
