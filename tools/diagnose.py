#!/usr/bin/env python3
"""Diagnostic data collector — gathers rich context for self-improvement.

Collects structured diagnostic data from multiple sources:
  - action_logs DB (procedure stats, failure patterns, recent attempts)
  - anima.log (filtered: recent server messages, errors, planner decisions)
  - state.json (current agent state, inventory, nearby NPCs)
  - improvements.jsonl (past fix attempts and their outcomes)

Produces a single diagnostic report dict that can be rendered as a prompt.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
LOG_FILE = ROOT / "data" / "anima.log"
STATE_FILE = ROOT / "data" / "state.json"
DB_FILE = ROOT / "data" / "anima.db"
IMPROVEMENTS_FILE = ROOT / "data" / "improvements.jsonl"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def collect(minutes: int = 10) -> dict:
    """Collect all diagnostic data into a single dict."""
    return {
        "timestamp": datetime.now().isoformat(),
        "agent_state": _read_agent_state(),
        "procedure_stats": _query_procedure_stats(minutes),
        "recent_failures": _query_recent_failures(minutes, limit=15),
        "failure_streaks": _detect_failure_streaks(minutes),
        "recent_log_lines": _extract_log_lines(minutes),
        "server_messages": _extract_server_messages(minutes),
        "planner_decisions": _extract_planner_logs(minutes),
        "past_fix_attempts": _read_past_fixes(limit=10),
        "constraints": _detect_constraints(),
    }


def render_prompt(diag: dict) -> str:
    """Render diagnostic data into a Claude Code prompt."""
    sections = []

    # --- Agent State ---
    st = diag.get("agent_state") or {}
    status = st.get("status", {})
    inv = st.get("inventory", [])
    inv_str = "\n".join(f"  - {i['amount']}x {i['name']}" for i in inv) or "  (empty)"
    sections.append(f"""## Agent State
- Position: ({status.get('x',0)}, {status.get('y',0)}, z={status.get('z',0)})
- HP: {status.get('hp',0)}/{status.get('hp_max',0)}
- Gold: {status.get('gold',0)}, Weight: {status.get('weight',0)}/{status.get('weight_max',0)}
- Intent: {status.get('intent', 'none')}
- Procedure: {status.get('procedure', 'none')}
- Inventory:
{inv_str}""")

    # --- Procedure Stats ---
    stats = diag.get("procedure_stats", [])
    if stats:
        lines = ["## Procedure Stats (last {0} min)".format(
            diag.get("_minutes", 10))]
        lines.append("| Procedure | Success | Fail | Rate | Avg ms | Top failure |")
        lines.append("|-----------|---------|------|------|--------|-------------|")
        for s in stats:
            rate = f"{s['success_rate']:.0%}" if s['total'] > 0 else "N/A"
            lines.append(
                f"| {s['procedure']} | {s['success']} | {s['fail']} | "
                f"{rate} | {s['avg_ms']:.0f} | {s['top_failure']} |")
        sections.append("\n".join(lines))

    # --- Failure Streaks (MOST IMPORTANT) ---
    streaks = diag.get("failure_streaks", [])
    if streaks:
        lines = ["## Failure Patterns (consecutive failures)"]
        for s in streaks:
            lines.append(f"\n### {s['procedure']} — {s['count']}x consecutive failures")
            lines.append(f"Failure type: `{s['top_reason']}`")
            lines.append("Recent messages:")
            for msg in s.get("messages", [])[:5]:
                lines.append(f"  - {msg}")
        sections.append("\n".join(lines))

    # --- Recent Failures (detail) ---
    failures = diag.get("recent_failures", [])
    if failures:
        lines = ["## Recent Failure Log (newest first)"]
        for f in failures[:10]:
            ts = datetime.fromtimestamp(f["timestamp"]).strftime("%H:%M:%S")
            lines.append(
                f"- [{ts}] {f['procedure']} → {f['result']}: {f['message'][:120]}")
        sections.append("\n".join(lines))

    # --- Server Messages ---
    srv = diag.get("server_messages", [])
    if srv:
        # Deduplicate with counts
        counts: dict[str, int] = {}
        for m in srv:
            counts[m] = counts.get(m, 0) + 1
        lines = ["## Server Messages (deduplicated)"]
        for msg, cnt in sorted(counts.items(), key=lambda x: -x[1])[:15]:
            lines.append(f"- ({cnt}x) {msg}")
        sections.append("\n".join(lines))

    # --- Planner Decisions ---
    planner = diag.get("planner_decisions", [])
    if planner:
        lines = ["## Planner Decision Log (last 10)"]
        for p in planner[-10:]:
            lines.append(f"- {p}")
        sections.append("\n".join(lines))

    # --- Constraints ---
    constraints = diag.get("constraints", {})
    if any(constraints.values()):
        lines = ["## Constraints (what the agent CANNOT do right now)"]
        for k, v in constraints.items():
            if v:
                lines.append(f"- {k}: {v}")
        sections.append("\n".join(lines))

    # --- Past Fix Attempts ---
    past = diag.get("past_fix_attempts", [])
    if past:
        lines = ["## Past Fix Attempts (most recent)"]
        for p in past:
            status = "SUCCESS" if p.get("code_changed") else "FAILED"
            lines.append(
                f"- [{p.get('ts')}] {p.get('action')} — {status}: "
                f"{p.get('reason', '')[:80]}")
            if p.get("output_preview"):
                lines.append(f"  Output: {p['output_preview'][:120]}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Data collectors
# ---------------------------------------------------------------------------

def _read_agent_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


def _query_procedure_stats(minutes: int) -> list[dict]:
    if not DB_FILE.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_FILE))
        cutoff = time.time() - minutes * 60
        rows = conn.execute("""
            SELECT procedure, result, COUNT(*) as cnt, ROUND(AVG(duration_ms)) as avg_ms
            FROM action_logs WHERE timestamp > ?
            GROUP BY procedure, result ORDER BY cnt DESC
        """, (cutoff,)).fetchall()
        conn.close()

        # Aggregate by procedure
        procs: dict[str, dict] = {}
        for proc, result, cnt, avg_ms in rows:
            if proc not in procs:
                procs[proc] = {"procedure": proc, "success": 0, "fail": 0,
                               "total": 0, "avg_ms": 0, "top_failure": "",
                               "_fail_types": {}}
            procs[proc]["total"] += cnt
            if result == "success":
                procs[proc]["success"] += cnt
            else:
                procs[proc]["fail"] += cnt
                procs[proc]["_fail_types"][result] = \
                    procs[proc]["_fail_types"].get(result, 0) + cnt
            procs[proc]["avg_ms"] = avg_ms or 0

        result = []
        for p in procs.values():
            p["success_rate"] = p["success"] / max(1, p["total"])
            if p["_fail_types"]:
                p["top_failure"] = max(p["_fail_types"], key=p["_fail_types"].get)
            del p["_fail_types"]
            result.append(p)
        return sorted(result, key=lambda x: x["fail"], reverse=True)
    except Exception:
        return []


def _query_recent_failures(minutes: int, limit: int = 30) -> list[dict]:
    if not DB_FILE.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_FILE))
        cutoff = time.time() - minutes * 60
        rows = conn.execute("""
            SELECT timestamp, procedure, result, message, location_x, location_y,
                   duration_ms, details
            FROM action_logs
            WHERE timestamp > ? AND result != 'success'
            ORDER BY timestamp DESC LIMIT ?
        """, (cutoff, limit)).fetchall()
        conn.close()
        return [
            {"timestamp": r[0], "procedure": r[1], "result": r[2],
             "message": r[3], "x": r[4], "y": r[5], "duration_ms": r[6],
             "details": r[7]}
            for r in rows
        ]
    except Exception:
        return []


def _detect_failure_streaks(minutes: int) -> list[dict]:
    """Find procedures that are failing consecutively (stuck loops)."""
    if not DB_FILE.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_FILE))
        cutoff = time.time() - minutes * 60
        # No LIMIT — time filter is sufficient. DESC to count from newest.
        rows = conn.execute("""
            SELECT procedure, result, message
            FROM action_logs WHERE timestamp > ?
            ORDER BY timestamp DESC
        """, (cutoff,)).fetchall()
        conn.close()

        # Count consecutive failures from most recent per procedure
        streaks: dict[str, dict] = {}
        for proc, result, msg in rows:
            if proc not in streaks:
                streaks[proc] = {"procedure": proc, "count": 0,
                                 "messages": [], "top_reason": "", "active": True}
            s = streaks[proc]
            if not s["active"]:
                continue
            if result != "success":
                s["count"] += 1
                if msg and msg not in s["messages"] and len(s["messages"]) < 5:
                    s["messages"].append(msg[:150])
                s["top_reason"] = result
            else:
                s["active"] = False  # streak broken by success

        return [s for s in streaks.values() if s["count"] >= 3]
    except Exception:
        return []


def _extract_log_lines(minutes: int) -> list[str]:
    """Extract important log lines (errors, warnings, stuck events)."""
    if not LOG_FILE.exists():
        return []

    cutoff = datetime.now() - timedelta(minutes=minutes)
    important = []

    try:
        with open(LOG_FILE) as f:
            for raw in f:
                line = _ANSI_RE.sub("", raw).strip()
                ts = _parse_ts(line)
                if ts is None or ts < cutoff:
                    continue
                # Keep errors, warnings, and key events
                if any(k in line for k in (
                    "[error", "[warning", "stuck", "deadlock", "gave_up",
                    "no_material", "too_far", "los_fail", "connection_lost",
                    "repeat_failure", "idle_warning",
                )):
                    important.append(line[:200])

        return important[-15:]  # last 15 important lines
    except Exception:
        return []


def _extract_server_messages(minutes: int) -> list[str]:
    """Extract server speech/cliloc messages."""
    if not LOG_FILE.exists():
        return []

    cutoff = datetime.now() - timedelta(minutes=minutes)
    messages = []

    try:
        with open(LOG_FILE) as f:
            for raw in f:
                line = _ANSI_RE.sub("", raw).strip()
                ts = _parse_ts(line)
                if ts is None or ts < cutoff:
                    continue
                if "speech_cliloc" in line and "text=" in line:
                    text = line.split("text=", 1)[1].strip().strip("'\"")
                    if text:
                        messages.append(text)

        return messages[-15:]
    except Exception:
        return []


def _extract_planner_logs(minutes: int) -> list[str]:
    """Extract planner decision logs."""
    if not LOG_FILE.exists():
        return []

    cutoff = datetime.now() - timedelta(minutes=minutes)
    entries = []

    try:
        with open(LOG_FILE) as f:
            for raw in f:
                line = _ANSI_RE.sub("", raw).strip()
                ts = _parse_ts(line)
                if ts is None or ts < cutoff:
                    continue
                if any(k in line for k in (
                    "planner_selected", "planner_result", "planner_state",
                    "planner_move_to", "planner_idle", "planner_deadlock",
                    "planner_sell", "procedure_result",
                )):
                    # Compact format: just event + key info
                    entries.append(line[24:200])  # skip timestamp prefix

        return entries[-15:]
    except Exception:
        return []


def _read_past_fixes(limit: int = 10) -> list[dict]:
    """Read past improvement attempts."""
    if not IMPROVEMENTS_FILE.exists():
        return []
    try:
        entries = []
        with open(IMPROVEMENTS_FILE) as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        # Return most recent, excluding auto_recover (just noise)
        relevant = [e for e in entries if e.get("action") != "auto_recover"]
        return relevant[-limit:]
    except Exception:
        return []


def _detect_constraints() -> dict[str, str]:
    """Detect what the agent CANNOT do right now."""
    state = _read_agent_state()
    if not state:
        return {}

    constraints = {}
    status = state.get("status", {})
    inv = state.get("inventory", [])
    inv_names = {i["name"].lower() for i in inv}

    gold = status.get("gold", 0)
    weight = status.get("weight", 0)
    weight_max = status.get("weight_max", 1)

    if gold < 10:
        constraints["no_gold"] = f"Gold={gold} — cannot buy tools or materials"
    if weight > weight_max * 0.9:
        constraints["overweight"] = f"Weight {weight}/{weight_max} — cannot carry more"
    if not any("pickaxe" in n or "shovel" in n for n in inv_names):
        constraints["no_mining_tool"] = "No pickaxe or shovel — cannot mine"
    if not any("tong" in n for n in inv_names):
        constraints["no_tongs"] = "No tongs — cannot craft blacksmith items"

    # True deadlock check
    if (constraints.get("no_gold") and constraints.get("no_mining_tool")
            and not any("ingot" in n for n in inv_names)
            and not any("ore" in n for n in inv_names)):
        constraints["DEADLOCK"] = (
            "No tools, no gold, no materials. "
            "Cannot mine, craft, buy, or sell. "
            "CODE FIX WON'T HELP — agent needs behavioral strategy change "
            "(e.g., ask for help, find items on ground, go to different area)."
        )

    return constraints


def _parse_ts(line: str) -> datetime | None:
    m = _TS_RE.search(line)
    if m:
        try:
            utc_ts = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
            return utc_ts.astimezone().replace(tzinfo=None)
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Collect diagnostic data")
    parser.add_argument("--minutes", type=int, default=10)
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    diag = collect(minutes=args.minutes)
    if args.json:
        print(json.dumps(diag, indent=2, default=str))
    else:
        print(render_prompt(diag))
