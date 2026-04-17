# Metrics Pipeline — Design Spec

**Status:** Proposed
**Date:** 2026-04-17
**Sub-project:** A1 (Foundation — Metrics)
**Parent plan:** Self-improvement overhaul — Phase A (Foundation) → B (Observability) → C (Autonomy) → D (Knowledge)

## Problem

The supervisor's 3-tier self-improvement loop runs hundreds of times per day, but no single number today answers: *"is the agent getting better, worse, or the same?"* There is no gold-per-hour, no cycles-per-day, no deaths-per-day, no procedure success-rate trend. Every analysis dip requires grepping a 500 MB log file by hand. Regressions after commits — for example, when the LLM strategy began blocking mining in a loop — were only discovered because the user happened to watch the agent at the right moment.

Without a ground truth for "good," improvement is guesswork.

## Goals

- Capture every meaningful agent event as structured rows.
- Roll events up into hourly and daily snapshots that answer "how did the last hour/day look?" in one JSON line each.
- Surface the hourly summary on the supervisor's stdio feed so live operators see outcomes.
- Offer a CLI for ad-hoc queries (`--today`, `--last 7d`, `--compare`).
- Fire a stdio warning when a metric regresses sharply (no automatic remediation yet — Phase C territory).

## Non-Goals

- No LLM or Claude invocation from this pipeline. Alerts are stdio-only here; Phase C adds autonomous response.
- No web dashboard. Terminal output only.
- No predictive analytics, anomaly ML, or RL. Simple threshold rules.
- No new SQLite tables. `action_logs` is reused; new streams live in JSONL files.
- No git-bisect integration. Commit correlation is manual for now.

## Approach — Hybrid collection

Three data sources feed a single aggregator:

1. **Automatic — state.json diff.** A collector polls the existing state snapshot every 5 s, diffs against the prior snapshot, and emits `gold_delta`, `inventory_delta`, `skill_delta`, `death` events without touching procedure code.
2. **Automatic — action_logs.** The existing SQLite table already records every procedure run. Hourly aggregation queries it directly for per-procedure success/fail counts and durations.
3. **Explicit — EventBus + API.** A handful of events need explicit emit because they have no equivalent in state.json or action_logs (expedition cycle complete, supervisor auto-recover, arbitrary custom events).

This keeps agent-side code changes minimal while guaranteeing accurate counts where passive observation isn't sufficient.

## Data Model

### Raw event stream
`data/metrics_events.jsonl` — one JSON line per event, append-only, 30-day rolling retention.

Event types (MVP):

| Type | Source | Payload |
|---|---|---|
| `procedure_end` | action_logs diff | `proc`, `success`, `duration_ms`, `reason` |
| `gold_delta` | state.json diff | `amount` (+/-), `reason` (best-effort string) |
| `cycle_complete` | bus `expedition.cycle_complete` | `cycles_completed`, `duration_s` |
| `phase_transition` | bus `expedition_phase` log event | `from`, `to` |
| `death` | bus `planner.death` | `pos`, `hp_before` |
| `skill_delta` | state.json diff | `skill`, `from`, `to` |
| `stuck_event` | bus `supervisor.auto_recover` (new) | `reason` |
| `inventory_delta` | state.json diff | `graphic`, `from`, `to` |

Every event has `ts` (float seconds since epoch) and `type`. Extra keys are type-specific.

Example lines:
```json
{"ts": 1776200000.0, "type": "procedure_end", "proc": "mine_ore", "success": true, "duration_ms": 3624}
{"ts": 1776200030.0, "type": "gold_delta", "amount": 28, "reason": "sell_to_vendor"}
{"ts": 1776200060.0, "type": "cycle_complete", "cycles_completed": 3, "duration_s": 847.2}
{"ts": 1776200090.0, "type": "death", "pos": [2553, 496], "hp_before": 26}
{"ts": 1776200120.0, "type": "skill_delta", "skill": "blacksmith", "from": 63.9, "to": 64.0}
```

### Hourly rollup
`data/metrics_hourly.jsonl` — one row per hour, permanent retention.

```json
{
  "hour": "2026-04-17T13:00:00Z",
  "uptime_s": 3600,
  "procedures": {
    "mine_ore": {"ok": 47, "fail": 5, "avg_ms": 3800},
    "smelt_ore": {"ok": 12, "fail": 1, "avg_ms": 2100}
  },
  "cycles_completed": 2,
  "phase_transitions": {"mining->collecting": 2, "collecting->mining": 2},
  "gold": {"start": 61, "end": 128, "delta": 67, "earned": 85, "spent": 18},
  "deaths": 0,
  "stuck_events": 1,
  "skills": {"blacksmith": {"from": 64.0, "to": 64.2}},
  "inventory_peaks": {"ingots": 24, "ore": 11}
}
```

### Daily rollup
`data/metrics_daily.jsonl` — one row per calendar day (UTC), permanent retention.

```json
{
  "date": "2026-04-17",
  "uptime_s": 68400,
  "cycles_total": 8,
  "gold_earned": 340,
  "gold_spent": 75,
  "net_gold": 265,
  "deaths": 1,
  "stuck_events": 12,
  "procedure_success_rate": 0.87,
  "top_failures": [["craft_blacksmith", 23], ["make_tools", 11]],
  "skills_gained": {"blacksmith": 1.2, "mining": 0.0},
  "auto_recover_count": 24,
  "hourly_missing": false
}
```

## Components

### `anima/monitor/metrics.py` (new)

Three classes plus a module-level `record()` helper.

**`MetricsCollector`**
- `start()` — launches background tasks:
  - bus subscriptions for `expedition.cycle_complete`, `planner.death`, `expedition_phase`, `supervisor.auto_recover`
  - state-poller loop (5 s cadence) computing diffs
  - action_logs poller (every 30 s) appending `procedure_end` events for any new action_log rows since last checkpoint
- `record(type: str, **payload)` — module-level function, appends a JSON line to `metrics_events.jsonl` atomically (open in append mode, single-line write, OS guarantees append atomicity for <PIPE_BUF bytes).

**`MetricsAggregator`**
- `run()` — asyncio task; waits for each wall-clock hour boundary, runs `build_hourly()`, waits for midnight UTC, runs `build_daily()`, applies retention trim.
- `build_hourly(window_start, window_end)` — reads events + action_logs in window, emits one row to `metrics_hourly.jsonl`, publishes `metrics.hourly_complete` on bus for supervisor.
- `build_daily(date)` — reads yesterday's hourly rows, emits one row to `metrics_daily.jsonl`, publishes `metrics.daily_complete`.
- `trim_events(cutoff_ts)` — rewrites `metrics_events.jsonl` into `.tmp`, drops events older than cutoff, renames atomically.

**`MetricsAlertDetector`**
- Subscribed to `metrics.hourly_complete`.
- Rule set:
  - `cycles_per_hour` ≤ 0.5 × trailing-6h-mean for 3 hours straight → alert
  - `deaths > 0` in any hourly row → alert
  - `stuck_events > 5` in any hourly row → alert
  - `procedure_success_rate < 0.6` for 2 hours straight → alert
- Writes `data/metrics_alerts.jsonl` and publishes `metrics.alert` bus event (supervisor surfaces on stdio).

### `anima/core/avatar.py` (modified)
Adds a single startup block: instantiate `MetricsCollector`, `MetricsAggregator`, `MetricsAlertDetector`, start their background tasks. Three new lines in the init flow.

### `anima/planner/planner.py` (modified, minimal)
The existing `expedition_cycle_complete` log call already publishes a bus event — collector subscribes, no code change needed. One-line additions where missing:
- Supervisor's `auto_recover()` adds `bus.publish("supervisor.auto_recover", {"reason": reason})` before restart.

### `tools/metrics.py` (new)
CLI entrypoint. Argparse commands:
- `--today` — print today's accumulated hourly rollups so far.
- `--hour now` — print partial current-hour stats from live events.
- `--last N{h|d}` — print last N hours/days of rollups.
- `--compare A B` — diff two windows (e.g., `today yesterday`, `7d 14d`).
- `--top-failures W` — procedures ranked by fail count in window W.
- `--json` — raw JSON instead of table.

Reads directly from JSONL files, no live agent needed.

### `tools/supervisor.py` (modified)
Subscribe to `metrics.hourly_complete` and `metrics.alert` bus topics. Format hourly rollup as a single stdio line (`[HH:MM] HOUR: cycles=2 gold=+67 deaths=0 stuck=1 proc_ok=93%`). Alerts prefixed with `⚠ METRIC ALERT:`.

## Data Flow

```
state.json ─────diff every 5s─────┐
action_logs ──poll every 30s─────┤
bus (expedition/planner/sup)─────┼──► MetricsCollector.record()
explicit record() calls──────────┘        │
                                          ▼
                         data/metrics_events.jsonl
                                          │
        ┌─────────────────────────────────┤
        │                                 │
 hourly tick                        daily tick (00:00 UTC)
        │                                 │
        ▼                                 ▼
 build_hourly()                     build_daily()
 reads: events + action_logs        reads: hourly rows of yesterday
 writes: metrics_hourly.jsonl       writes: metrics_daily.jsonl
        │                                 trim_events(cutoff=now-30d)
        ▼
 publish metrics.hourly_complete
        │
        ├──► AlertDetector (rules → metrics.alert if bad)
        └──► supervisor.py → stdio formatter
```

## Failure Handling

- **State poller exception** — log warning, keep running; never let metrics break the agent.
- **Bus unavailable** — collector's `record()` still writes file directly; bus is only for fan-out to aggregator/supervisor.
- **File I/O error on events.jsonl** — log warning, drop the event. Losing a single event is cheaper than a crash.
- **Missing hourly rollup** (e.g., agent restart spanning the hour boundary) — detected on next tick: if `last(hourly).hour < now - 2h`, backfill missing hours from events. Mark as `backfilled: true`.
- **Trim failure** — on `.tmp → .jsonl` rename failure, keep the original; retry next daily tick.
- **Retention edge** — never trim the events file while `build_hourly` is reading it. Acquire a simple in-process lock (both run in the same event loop so coordination is trivial).

## Concurrency

Everything runs in the agent's single asyncio event loop. No threads, no locks beyond the retention/rollup coordination above. `record()` is called from many callers but only writes via append-mode file writes (OS-atomic for small lines).

## Observability

- Structured logs: `metrics_event_written`, `metrics_hourly_built`, `metrics_daily_built`, `metrics_alert_fired`, `metrics_trim_complete`.
- Health: `MetricsCollector` exposes `last_event_ts` attribute so supervisor can detect "metrics pipeline frozen" (no events in 10 min while agent is alive).

## Testing

- `tests/monitor/test_metrics_collector.py`
  - state.json diff — gold change produces one `gold_delta` event
  - state.json diff — HP 0 transition produces one `death` event (and not twice on stay-dead)
  - bus subscription — `expedition.cycle_complete` event yields `cycle_complete` row
  - explicit `record()` call appends correctly
- `tests/monitor/test_metrics_aggregator.py`
  - `build_hourly()` on a fixed event set produces expected rollup
  - `build_daily()` on fixed hourly rows
  - `trim_events()` removes only rows older than cutoff
  - backfill after missing hour
- `tests/monitor/test_metrics_alerts.py`
  - 3-hour rolling window triggers cycles regression
  - single death fires immediately
  - boundary values (exactly 50%, exactly 0) behave as specified
- `tests/tools/test_metrics_cli.py`
  - `--today` output with empty files
  - `--compare today yesterday` math
  - `--top-failures 24h` sort order

## Rollout

1. Ship `anima/monitor/metrics.py` with `MetricsCollector` + `record()` + unit tests.
2. Ship `MetricsAggregator` + hourly/daily rollup + retention + unit tests.
3. Wire bus subscribers + avatar.py startup + one-line emit points in planner/supervisor.
4. Ship `tools/metrics.py` CLI.
5. Ship `MetricsAlertDetector` + supervisor stdio formatter.
6. Observe for 48 hours, tune alert thresholds based on first real data.

Each step commits independently; the agent continues to run without the pipeline until step 3, and the pipeline runs passively after that.

## Completion Criteria

- After 48 hours of agent uptime, `metrics_daily.jsonl` has 2 rows with sensible values for every field.
- `tools/metrics.py --today` returns a table without errors.
- At least one stdio hourly summary appears on the supervisor feed.
- Deliberate regression test: introduce a bug that blocks mining → within one hour, alert fires on `cycles_per_hour` drop.
- No measurable overhead: agent tick latency unchanged (<1 ms metrics overhead per tick).
