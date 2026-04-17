#!/usr/bin/env python3
"""Metrics CLI — query hourly/daily rollups and raw events.

Examples:
  tools/metrics.py --today
  tools/metrics.py --last 7d
  tools/metrics.py --top-failures 24h
  tools/metrics.py --compare today yesterday
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _path(env_key: str, default_name: str) -> Path:
    return Path(os.environ.get(env_key) or (ROOT / "data" / default_name))


HOURLY_FILE = lambda: _path("ANIMA_METRICS_HOURLY", "metrics_hourly.jsonl")
DAILY_FILE = lambda: _path("ANIMA_METRICS_DAILY", "metrics_daily.jsonl")
EVENTS_FILE = lambda: _path("ANIMA_METRICS_EVENTS", "metrics_events.jsonl")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _rows_for_today(hourly: list[dict]) -> list[dict]:
    prefix = _today_iso()
    return [r for r in hourly if r.get("hour", "").startswith(prefix)]


def _parse_window(text: str) -> tuple[float, float]:
    """Parse a window string like '24h' or '7d' into (start_ts, end_ts)."""
    now = time.time()
    try:
        if text.endswith("h"):
            return now - int(text[:-1]) * 3600, now
        if text.endswith("d"):
            return now - int(text[:-1]) * 86400, now
    except ValueError:
        pass
    raise SystemExit(f"Invalid window: {text!r}")


def cmd_today(args) -> int:
    hourly = _read_jsonl(HOURLY_FILE())
    rows = _rows_for_today(hourly)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print(f"No data yet for {_today_iso()}")
        return 0
    print(f"Today ({_today_iso()}) — {len(rows)} hour(s):")
    print(f"{'hour':<17} {'cyc':>4} {'gold':>6} {'deaths':>6} {'stuck':>6} {'ok%':>5}")
    for r in rows:
        hour = r.get("hour", "")[:16].replace("T", " ")
        cyc = r.get("cycles_completed", 0)
        g = r.get("gold", {}).get("delta", 0)
        deaths = r.get("deaths", 0)
        stuck = r.get("stuck_events", 0)
        ok = sum(p.get("ok", 0) for p in r.get("procedures", {}).values())
        fail = sum(p.get("fail", 0) for p in r.get("procedures", {}).values())
        rate = (ok / (ok + fail) * 100) if (ok + fail) else 100.0
        print(f"{hour:<17} {cyc:>4} {g:+6d} {deaths:>6} {stuck:>6} {rate:>4.0f}%")
    return 0


def cmd_last(args) -> int:
    start, end = _parse_window(args.last)
    hourly = _read_jsonl(HOURLY_FILE())
    rows = [
        r for r in hourly
        if start <= _parse_hour(r.get("hour", "")) < end
    ]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    print(f"Last {args.last} — {len(rows)} rows")
    for r in rows:
        hour = r.get("hour", "")[:16].replace("T", " ")
        print(f"  {hour}  cycles={r.get('cycles_completed', 0)}  "
              f"gold={r.get('gold', {}).get('delta', 0):+d}  "
              f"deaths={r.get('deaths', 0)}")
    return 0


def cmd_top_failures(args) -> int:
    start, end = _parse_window(args.top_failures)
    hourly = _read_jsonl(HOURLY_FILE())
    rows = [
        r for r in hourly
        if start <= _parse_hour(r.get("hour", "")) < end
    ]
    fail_count: dict[str, int] = defaultdict(int)
    for r in rows:
        for proc, stats in r.get("procedures", {}).items():
            fail_count[proc] += stats.get("fail", 0)
    ranked = sorted(fail_count.items(), key=lambda kv: (-kv[1], kv[0]))
    if args.json:
        print(json.dumps(ranked, ensure_ascii=False))
        return 0
    print(f"Top failures in last {args.top_failures}:")
    for proc, c in ranked[:10]:
        print(f"  {proc:<30} {c}")
    return 0


def cmd_compare(args) -> int:
    a_label, b_label = args.compare
    hourly = _read_jsonl(HOURLY_FILE())
    daily = _read_jsonl(DAILY_FILE())

    def _stats_for_day(label: str) -> dict:
        if label == "today":
            rows = _rows_for_today(hourly)
            return {
                "cycles": sum(r.get("cycles_completed", 0) for r in rows),
                "gold_delta": sum(r.get("gold", {}).get("delta", 0) for r in rows),
                "deaths": sum(r.get("deaths", 0) for r in rows),
                "stuck": sum(r.get("stuck_events", 0) for r in rows),
            }
        if label == "yesterday":
            y = (
                datetime.now(timezone.utc).date() - timedelta(days=1)
            ).isoformat()
            for d in daily:
                if d.get("date") == y:
                    return {
                        "cycles": d.get("cycles_total", 0),
                        "gold_delta": d.get("net_gold", 0),
                        "deaths": d.get("deaths", 0),
                        "stuck": d.get("stuck_events", 0),
                    }
            return {"cycles": 0, "gold_delta": 0, "deaths": 0, "stuck": 0}
        raise SystemExit(f"Unsupported compare label: {label}")

    a = _stats_for_day(a_label)
    b = _stats_for_day(b_label)
    if args.json:
        print(json.dumps({a_label: a, b_label: b}, ensure_ascii=False))
        return 0
    print(f"{'metric':<15} {a_label:>10} {b_label:>10} {'delta':>10}")
    for key in ("cycles", "gold_delta", "deaths", "stuck"):
        print(f"{key:<15} {a[key]:>10} {b[key]:>10} {a[key] - b[key]:>+10}")
    return 0


def _parse_hour(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


def main() -> int:
    p = argparse.ArgumentParser(prog="metrics", description=__doc__)
    p.add_argument("--today", action="store_true", help="Today's hourly rollups")
    p.add_argument("--last", metavar="Nh|Nd", help="Last N hours/days of hourly rows")
    p.add_argument("--top-failures", metavar="WINDOW", help="Top failing procedures in window")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"), help="Compare two windows")
    p.add_argument("--json", action="store_true", help="Raw JSON output")
    args = p.parse_args()

    if args.today:
        return cmd_today(args)
    if args.last:
        return cmd_last(args)
    if args.top_failures:
        return cmd_top_failures(args)
    if args.compare:
        return cmd_compare(args)
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
