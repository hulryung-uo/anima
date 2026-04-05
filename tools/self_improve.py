#!/usr/bin/env python3
"""Self-improvement loop — analyze logs, generate plan, optionally call Claude Code.

Usage:
    # One-shot analysis
    uv run python tools/self_improve.py

    # Continuous mode (run every 10 minutes)
    uv run python tools/self_improve.py --loop

    # Call Claude Code for complex fixes
    uv run python tools/self_improve.py --loop --claude
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Match ISO timestamps like 2026-03-21T03:42:18.123456Z
_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")

ROOT = Path(__file__).parent.parent
LOG_FILE = ROOT / "data" / "anima.log"
ANALYSIS_DIR = ROOT / "data" / "analysis"
PLANS_DIR = ROOT / "data" / "plans"


def _parse_ts(line: str) -> datetime | None:
    """Extract ISO timestamp from a log line.

    structlog writes UTC timestamps (suffix Z), so we treat parsed
    timestamps as UTC and convert to local time for comparison with
    datetime.now().
    """
    m = _TS_RE.search(line)
    if m:
        try:
            utc_ts = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
            return utc_ts.astimezone().replace(tzinfo=None)
        except ValueError:
            pass
    return None


def parse_recent_log(minutes: int = 10) -> dict:
    """Parse recent log entries and extract metrics."""
    if not LOG_FILE.exists():
        return {"error": "No log file found"}

    cutoff = datetime.now() - timedelta(minutes=minutes)

    counts: dict[str, int] = {
        "walk_confirmed": 0,
        "walk_denied": 0,
        "chop_success": 0,
        "chop_fail": 0,
        "chop_depleted": 0,
        "chop_unreachable": 0,
        "make_boards_success": 0,
        "carpentry_success": 0,
        "carpentry_fail": 0,
        "carpentry_need_materials": 0,
        "carpentry_tool_broke": 0,
        "tinker_craft_success": 0,
        "tinker_craft_failed": 0,
        "smelt_success": 0,
        "mine_success": 0,
        "mine_fail": 0,
        "vendor_buy_sent": 0,
        "vendor_sell_sent": 0,
        "bank_deposit_success": 0,
        "skill_gain": 0,
        "skill_executing": 0,
        "stuck": 0,
        "escape_stuck": 0,
        "think_decided": 0,
        "go_to_no_path": 0,
        "smelt_walking_to_forge": 0,
        "mine_walking_to": 0,
        # v2 planner/procedure counts
        "planner_selected": 0,
        "planner_no_procedure_available": 0,
        "planner_move_to": 0,
        "planner_restock": 0,
        "procedure_result": 0,
        "mine_depleted": 0,
        "door_opened": 0,
        "go_to_arrived": 0,
        "go_to_denied": 0,
        "go_to_give_up": 0,
        "go_to_step": 0,
        "smelt_gave_up_ore": 0,
    }

    recent_goals: list[str] = []
    recent_problems: list[str] = []
    recent_server_msgs: list[str] = []
    recent_skill_logs: list[str] = []
    positions: set[str] = set()
    total_lines = 0
    recent_lines = 0

    with open(LOG_FILE) as f:
        for raw_line in f:
            total_lines += 1
            line = _ANSI_RE.sub("", raw_line)

            # Time-based filtering — only count lines with a valid timestamp
            ts = _parse_ts(line)
            if ts is None:
                continue  # skip non-timestamped noise (litellm verbose, etc.)
            if ts < cutoff:
                continue
            recent_lines += 1

            for key in counts:
                if key in line:
                    counts[key] += 1
                    break

            if "goal_set" in line and "place=" in line:
                try:
                    place = line.split("place=")[1].split()[0].strip("'\"")
                    recent_goals.append(place)
                except IndexError:
                    pass

            if "walk_denied" in line:
                positions.add(line.strip()[-60:])

            if "stuck" in line.lower() or "unreachable" in line:
                recent_problems.append(line.strip()[-120:])

            # Collect server messages (cliloc speech) for debugging context
            if "speech_cliloc" in line and "text=" in line:
                recent_server_msgs.append(line.strip()[-150:])

            # Collect skill/procedure execution logs
            if any(k in line for k in (
                "skill_executing", "mine_target", "mine_fail", "mine_success",
                "chop_target", "smelt_", "blacksmith_", "skill_problem",
                "context_menu", "vendor_",
                # v2 procedure logs
                "procedure_result", "planner_selected", "planner_restock",
                "planner_move_to", "mine_depleted", "planner_stuck",
                "picked_up_ore", "door_opened",
            )):
                recent_skill_logs.append(line.strip()[-150:])

    walk_total = counts["walk_confirmed"] + counts["walk_denied"]
    chop_total = counts["chop_success"] + counts["chop_fail"]
    skill_total = (
        counts["chop_success"] + counts["chop_fail"]
        + counts["carpentry_success"] + counts["carpentry_fail"]
        + counts["mine_success"] + counts["mine_fail"]
        + counts["tinker_craft_success"] + counts["tinker_craft_failed"]
    )
    skill_success = (
        counts["chop_success"] + counts["carpentry_success"]
        + counts["mine_success"] + counts["tinker_craft_success"]
        + counts["make_boards_success"] + counts["smelt_success"]
    )

    # Query action_logs DB for procedure stats
    db_stats = _query_action_logs(minutes)

    return {
        "counts": counts,
        "total_lines": total_lines,
        "recent_lines": recent_lines,
        "unique_deny_positions": len(positions),
        "recent_goals": recent_goals[-5:],
        "recent_problems": recent_problems[-10:],
        "recent_server_msgs": recent_server_msgs[-15:],
        "recent_skill_logs": recent_skill_logs[-20:],
        "walk_success_rate": counts["walk_confirmed"] / max(1, walk_total),
        "chop_success_rate": counts["chop_success"] / max(1, chop_total),
        "skill_success_rate": skill_success / max(1, skill_total),
        "minutes_analyzed": minutes,
        "db_stats": db_stats,
    }


def _query_action_logs(minutes: int) -> dict:
    """Query action_logs SQLite table for procedure performance stats."""
    import sqlite3
    db_path = ROOT / "data" / "anima.db"
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db_path))
        cutoff = time.time() - minutes * 60
        rows = conn.execute(
            """SELECT procedure, result, COUNT(*) as cnt,
                      ROUND(AVG(duration_ms)) as avg_ms
               FROM action_logs WHERE timestamp > ?
               GROUP BY procedure, result ORDER BY cnt DESC""",
            (cutoff,),
        ).fetchall()
        conn.close()
        stats = {}
        for proc, result, cnt, avg_ms in rows:
            key = f"{proc}:{result}"
            stats[key] = {"count": cnt, "avg_ms": int(avg_ms or 0)}
        return stats
    except Exception:
        return {}


def detect_problems(data: dict) -> list[dict]:
    """Detect problems from parsed log data."""
    problems = []
    counts = data.get("counts", {})

    # --- CRITICAL ---

    # No movement at all (tried but failed, or go_to_no_path loop)
    no_path = counts.get("go_to_no_path", 0)
    walk_ok = counts.get("walk_confirmed", 0)
    walk_deny = counts.get("walk_denied", 0)

    if walk_ok == 0 and walk_deny > 0:
        problems.append({
            "severity": "CRITICAL",
            "name": "cannot_move",
            "description": "No successful walks — completely stuck",
            "fix_type": "movement",
        })

    # Pathfinding loop — go_to_no_path repeating (agent frozen)
    if no_path > 20:
        problems.append({
            "severity": "CRITICAL",
            "name": "pathfinding_loop",
            "description": (
                f"go_to_no_path called {no_path} times — "
                f"agent cannot reach any destination"
            ),
            "fix_type": "movement",
        })

    # Agent doing nothing useful — many skill calls but zero results
    skill_exec = counts.get("skill_executing", 0)
    skill_rate = data.get("skill_success_rate", 1.0)
    if skill_exec > 50 and skill_rate == 0.0 and walk_ok == 0:
        problems.append({
            "severity": "CRITICAL",
            "name": "agent_frozen",
            "description": (
                f"{skill_exec} skill executions, 0% success, 0 walks — "
                f"agent is stuck in a loop"
            ),
            "fix_type": "brain",
        })

    # --- HIGH ---

    # Stuck in place (many denials, few positions)
    if walk_deny > 10 and data.get("unique_deny_positions", 0) < 5:
        problems.append({
            "severity": "HIGH",
            "name": "stuck_in_place",
            "description": (
                f"{walk_deny} denials, "
                f"{data['unique_deny_positions']} unique positions"
            ),
            "fix_type": "movement",
        })

    # Pathfinding failures (moderate)
    if 5 < no_path <= 20:
        problems.append({
            "severity": "HIGH",
            "name": "pathfinding_failures",
            "description": f"go_to_no_path {no_path} times",
            "fix_type": "movement",
        })

    # Thinking but not acting — even if walking, 0 skill execution is a problem
    think_count = counts.get("think_decided", 0)
    if think_count > 5 and skill_exec == 0:
        problems.append({
            "severity": "HIGH",
            "name": "thinking_not_acting",
            "description": (
                f"{think_count} think decisions, "
                f"0 skill executions, {walk_ok} walks — "
                f"walking but never doing any work"
            ),
            "fix_type": "brain",
        })

    # Repeating same goal — agent is going back and forth
    goals = data.get("recent_goals", [])
    if len(goals) >= 3:
        unique_goals = set(goals)
        if len(unique_goals) <= 2:
            problems.append({
                "severity": "HIGH",
                "name": "goal_loop",
                "description": (
                    f"Repeating same goals: {', '.join(unique_goals)} "
                    f"({len(goals)} times)"
                ),
                "fix_type": "brain",
            })

    # --- MEDIUM ---

    # Low walk success rate
    walk_rate = data.get("walk_success_rate", 1.0)
    walk_total = walk_ok + walk_deny
    if walk_total > 20 and walk_rate < 0.6:
        problems.append({
            "severity": "MEDIUM",
            "name": "low_walk_success",
            "description": f"Walk success rate: {walk_rate:.0%} ({walk_total} attempts)",
            "fix_type": "movement",
        })

    # No gathering while needing materials
    if counts.get("carpentry_need_materials", 0) > 3 and counts.get("chop_success", 0) == 0:
        problems.append({
            "severity": "MEDIUM",
            "name": "no_materials_gathered",
            "description": "Carpentry needs materials but no chopping success",
            "fix_type": "gathering",
        })

    # v2 planner idle — no procedures available for extended period
    no_proc = counts.get("planner_no_procedure_available", 0)
    proc_selected = counts.get("planner_selected", 0)
    if no_proc > 50 and proc_selected < 3:
        problems.append({
            "severity": "HIGH",
            "name": "planner_idle",
            "description": (
                f"Planner idle {no_proc} ticks, only {proc_selected} procedures selected. "
                f"Agent may be missing tools or stuck."
            ),
            "fix_type": "planner",
        })

    # v2 mine depleted loop — same area depleted repeatedly
    depleted = counts.get("mine_depleted", 0)
    if depleted > 10:
        problems.append({
            "severity": "MEDIUM",
            "name": "mine_exhausted",
            "description": f"Mine area depleted {depleted} times — should move to new area",
            "fix_type": "planner",
        })

    # v2 go_to failures
    go_give_up = counts.get("go_to_give_up", 0)
    if go_give_up > 5:
        problems.append({
            "severity": "HIGH",
            "name": "navigation_failure",
            "description": f"go_to gave up {go_give_up} times — pathfinding issues",
            "fix_type": "movement",
        })

    # v2: smelt failure loop — repeatedly failing to smelt
    smelt_gave_up = counts.get("smelt_gave_up_ore", 0)
    if smelt_gave_up > 3:
        problems.append({
            "severity": "MEDIUM",
            "name": "smelt_loop",
            "description": f"Gave up smelting ore {smelt_gave_up} times — picking up junk ore?",
            "fix_type": "planner",
        })

    # v2: procedure spam — many procedure calls but no movement
    proc_selected = counts.get("planner_selected", 0)
    go_steps = counts.get("go_to_step", 0)
    if proc_selected > 100 and go_steps == 0:
        problems.append({
            "severity": "HIGH",
            "name": "procedure_spam",
            "description": f"{proc_selected} procedures selected but 0 walk steps — stuck in loop",
            "fix_type": "planner",
        })

    # Tool breakage without replacement
    if counts.get("carpentry_tool_broke", 0) > 0 and counts.get("vendor_buy_sent", 0) == 0:
        problems.append({
            "severity": "MEDIUM",
            "name": "tool_broke_no_replacement",
            "description": "Tool broke but no vendor purchase attempted",
            "fix_type": "trade",
        })

    # Many skill executions but low success
    if skill_exec > 10 and skill_rate < 0.1:
        problems.append({
            "severity": "MEDIUM",
            "name": "low_skill_success",
            "description": f"Skill success rate: {skill_rate:.0%} ({skill_exec} executions)",
            "fix_type": "parameter",
        })

    # Many stuck events
    if counts.get("stuck", 0) > 5:
        problems.append({
            "severity": "MEDIUM",
            "name": "frequent_stuck",
            "description": f"{counts['stuck']} stuck events",
            "fix_type": "movement",
        })

    # Zero activity
    total_activity = sum(counts.values())
    if data.get("recent_lines", 0) > 10 and total_activity == 0:
        problems.append({
            "severity": "HIGH",
            "name": "no_activity",
            "description": "Agent appears idle — no recognizable events",
            "fix_type": "brain",
        })

    # --- DB-based failure detection ---
    # db_stats format: {"procedure:result": {"count": N, "avg_ms": M}}
    db_stats = data.get("db_stats", {})
    if db_stats:
        # Aggregate by procedure
        proc_agg: dict[str, dict[str, int]] = {}
        for key, info in db_stats.items():
            proc, result = key.split(":", 1)
            if proc not in proc_agg:
                proc_agg[proc] = {"success": 0, "fail": 0}
            if result == "success":
                proc_agg[proc]["success"] += info["count"]
            else:
                proc_agg[proc]["fail"] += info["count"]

        for proc, agg in proc_agg.items():
            total = agg["success"] + agg["fail"]
            if total < 5:
                continue
            fail_rate = agg["fail"] / total
            if fail_rate > 0.8:
                severity = "CRITICAL" if agg["success"] == 0 and total > 20 else "HIGH"
                problems.append({
                    "severity": severity,
                    "name": "db_procedure_failing",
                    "description": (
                        f"{proc} failing {fail_rate:.0%} "
                        f"({agg['fail']}/{total} attempts)"
                    ),
                    "fix_type": "procedure",
                })

    return problems


def generate_report(data: dict, problems: list[dict]) -> str:
    """Generate an analysis report as markdown."""
    ts = datetime.now()
    counts = data.get("counts", {})
    mins = data.get("minutes_analyzed", 10)

    lines = [
        f"# Analysis Report — {ts.strftime('%Y-%m-%d %H:%M')}",
        f"_Last {mins} minutes, {data.get('recent_lines', 0)} log lines_",
        "",
        "## Metrics",
        "",
        f"- Walk: {counts.get('walk_confirmed', 0)} OK / "
        f"{counts.get('walk_denied', 0)} denied "
        f"(rate: {data.get('walk_success_rate', 0):.0%})",
        f"- Chop: {counts.get('chop_success', 0)} OK / "
        f"{counts.get('chop_fail', 0)} fail "
        f"(rate: {data.get('chop_success_rate', 0):.0%})",
        f"- Boards made: {counts.get('make_boards_success', 0)}",
        f"- Carpentry: {counts.get('carpentry_success', 0)} OK / "
        f"{counts.get('carpentry_fail', 0)} fail",
        f"- Tinkering: {counts.get('tinker_craft_success', 0)} OK / "
        f"{counts.get('tinker_craft_failed', 0)} fail",
        f"- Mining: {counts.get('mine_success', 0)} OK / "
        f"{counts.get('mine_fail', 0)} fail",
        f"- Vendor buy: {counts.get('vendor_buy_sent', 0)}, "
        f"sell: {counts.get('vendor_sell_sent', 0)}",
        f"- Bank deposits: {counts.get('bank_deposit_success', 0)}",
        f"- Skill gains: {counts.get('skill_gain', 0)}",
        f"- Stuck events: {counts.get('stuck', 0)} "
        f"(escapes: {counts.get('escape_stuck', 0)})",
        f"- Think decisions: {counts.get('think_decided', 0)}",
        f"- Goals: {', '.join(data.get('recent_goals', [])) or 'none'}",
        "",
        "## v2 Planner Metrics",
        "",
        f"- Procedures selected: {counts.get('planner_selected', 0)}",
        f"- Procedure results: {counts.get('procedure_result', 0)}",
        f"- Planner idle ticks: {counts.get('planner_no_procedure_available', 0)}",
        f"- Move-to commands: {counts.get('planner_move_to', 0)}",
        f"- Restock trips: {counts.get('planner_restock', 0)}",
        f"- Mine depleted: {counts.get('mine_depleted', 0)}",
        f"- Doors opened: {counts.get('door_opened', 0)}",
        f"- Navigation: arrived={counts.get('go_to_arrived', 0)}, "
        f"denied={counts.get('go_to_denied', 0)}, "
        f"gave_up={counts.get('go_to_give_up', 0)}",
        "",
    ]

    # Append current agent state from state.json
    state_file = ROOT / "data" / "state.json"
    if state_file.exists():
        try:
            import json as _json
            state = _json.loads(state_file.read_text())
            s = state.get("status", {})
            lines.append("## Current Agent State")
            lines.append("")
            lines.append(f"- Position: ({s.get('x',0)}, {s.get('y',0)}, z={s.get('z',0)})")
            lines.append(f"- HP: {s.get('hp',0)}/{s.get('hp_max',0)}  Gold: {s.get('gold',0)}  Weight: {s.get('weight',0)}/{s.get('weight_max',0)}")
            inv = state.get("inventory", [])
            if inv:
                inv_str = ", ".join(f"{i['amount']}x {i['name']}" for i in inv[:8])
                lines.append(f"- Inventory: {inv_str}")
            act = state.get("activity", [])
            if act:
                lines.append("- Recent activity:")
                for a in act[-5:]:
                    lines.append(f"  - {a.get('message', '')}")
            lines.append(f"- Ping: {state.get('ping_ms', 0):.0f}ms")
            lines.append("")
        except Exception:
            pass

    # DB procedure stats
    db_stats = data.get("db_stats", {})
    if db_stats:
        lines.append("## Procedure Stats (from DB)")
        lines.append("")
        lines.append("| Procedure | Result | Count | Avg ms |")
        lines.append("|-----------|--------|-------|--------|")
        for key, info in sorted(db_stats.items(), key=lambda x: -x[1]["count"]):
            proc, result = key.split(":", 1)
            lines.append(f"| {proc} | {result} | {info['count']} | {info['avg_ms']} |")
        lines.append("")

    lines.append("## Problems")
    lines.append("")
    if problems:
        for p in problems:
            lines.append(
                f"- **[{p['severity']}]** {p['name']}: {p['description']}"
            )
    else:
        lines.append("No problems detected. Agent is healthy.")
    lines.append("")

    if problems:
        lines.append("## Suggested Actions")
        lines.append("")
        for p in problems:
            lines.append(f"### {p['name']} ({p['severity']})")
            lines.append(f"_{p['description']}_")
            if p["fix_type"] == "movement":
                lines.append("- Check pathfinding and denied_tiles cache")
                lines.append("- Try escape with larger radius")
                lines.append("- Consider moving to known safe location")
            elif p["fix_type"] == "gathering":
                lines.append("- Move to forest/mining area")
                lines.append("- Check weight limits")
                lines.append("- Verify tool availability")
            elif p["fix_type"] == "trade":
                lines.append("- Go to vendor to buy replacement tools")
                lines.append("- Check BuyFromNpc skill availability")
            elif p["fix_type"] == "brain":
                lines.append("- Check BT priority ordering")
                lines.append("- Verify skill_exec vs think interaction")
                lines.append("- Check if can_execute passes for any skills")
            elif p["fix_type"] == "parameter":
                lines.append("- Review skill cooldowns")
                lines.append("- Check can_execute conditions")
            lines.append("")

    if data.get("recent_problems"):
        lines.append("## Recent Error Lines")
        lines.append("")
        for prob in data["recent_problems"][-5:]:
            lines.append(f"- `{prob}`")
        lines.append("")

    if data.get("recent_server_msgs"):
        lines.append("## Recent Server Messages")
        lines.append("")
        # Deduplicate — show unique messages with counts
        msg_counts: dict[str, int] = {}
        for msg in data["recent_server_msgs"]:
            # Extract text= value
            if "text=" in msg:
                text = msg.split("text=", 1)[1].strip()
                msg_counts[text] = msg_counts.get(text, 0) + 1
            else:
                msg_counts[msg] = msg_counts.get(msg, 0) + 1
        for msg, cnt in sorted(msg_counts.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"- {msg} (x{cnt})")
        lines.append("")

    if data.get("recent_skill_logs"):
        lines.append("## Recent Skill Logs (sample)")
        lines.append("")
        for entry in data["recent_skill_logs"][-10:]:
            lines.append(f"- `{entry}`")
        lines.append("")

    return "\n".join(lines) + "\n"


def save_report(report: str) -> Path:
    """Save report to file."""
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    path = PLANS_DIR / f"{ts}_report.md"
    path.write_text(report)
    return path


def call_claude(report_path: Path) -> tuple[bool, str]:
    """Call Claude Code CLI to apply the improvement plan.

    Returns (success, output) — output contains Claude's stdout+stderr.
    """
    prompt = f"""You are the self-improvement system for Anima, a UO AI agent.
Read the analysis report at {report_path}, then debug and fix problems.

## Your debugging method

Follow the exact same process a human developer would use:

### Step 1: Read the analysis report and understand the situation
- Read {report_path} for problem summary, metrics, and server messages.
- Read data/state.json for current agent state.
- Read the last 200 lines of data/anima.log for raw log context.

### Step 2: Identify the problem pattern
Look for these patterns we've seen before:
- **Stuck loop**: same procedure failing repeatedly (e.g., mine_ore failing with
  "too far away" every 3 seconds = tile outside mining range but inside search range)
- **Wrong target**: selling to wrong vendor type (e.g., tanner for weapons = vendor
  doesn't buy those items, should go to weaponsmith)
- **NPC invisible**: vendor exists nearby but _find_vendor returns None (= NPC name
  not loaded, need OPL request before checking)
- **Craft failure ignored**: gump shows "You do not have sufficient metal" but code
  reports "Craft result unclear" (= gump notice text not being parsed)
- **Fall-through missing**: can_start false + _move_to_location returns None = infinite
  tick loop at 200ms without doing anything (= need fall-through to lower priority)
- **Unreachable destination**: go_to fails repeatedly for same coordinates (= wrong
  location data in world_knowledge.py, or building in the way)

### Step 3: Find the root cause in code
- Read the actual procedure/planner code that's failing.
- Check the can_start conditions — are they too loose or too strict?
- Check failure handling — does the code handle all server responses?
- Check for missing fall-through when both can_start and move fail.

### Step 4: Fix with minimal changes
- Fix the specific bug, don't refactor surrounding code.
- Add logging for the specific failure case so future debugging is easier.
- If a stuck loop: add blacklist/cooldown for the failing target.
- If wrong target: fix the matching logic or location data.
- If missing handling: add the specific server message to the handler.

## Architecture reference

- Planner: anima/planner/planner.py — rule-based procedure selection, priority 1-9
- Procedures: anima/procedures/*.py — async game workflows (mine_ore, smelt_ore,
  craft_blacksmith, sell_to_vendor, buy_from_vendor, make_tools, bank_deposit)
- Vendor logic: anima/skills/trade/vendor.py — _find_vendor, _is_vendor, context menu
- Movement: anima/action/movement.py — go_to() pathfinding
- World knowledge: anima/world_knowledge.py — named location coordinates
- Gump handling: anima/actions/gump.py — craft menu interaction

Gameplay loop: mine → smelt → craft → sell → bank → buy tools → mine

### Step 5: If root cause is unclear — add instrumentation first

If the logs don't contain enough information to diagnose the problem:
1. **Don't guess at a fix.** Instead, add targeted diagnostic logging.
2. Add logger.info() calls at the specific decision points that need visibility.
   For example: log the return value of _find_vendor, log the tile coordinates
   from _find_mineable_tile, log the gump notice text after crafting.
3. Commit the diagnostic logging with message like "Add diagnostic logging for X".
4. In the next self-improvement cycle, the new logs will reveal the root cause.

This is better than guessing — one cycle to instrument, next cycle to fix.

Examples of useful diagnostic logging:
- "vendor_search_result" with nearby NPC names, serials, distances, refused status
- "craft_gump_notice" with the full notice text from the gump
- "planner_decision" with why a specific priority branch was taken
- "tile_search_result" with coordinates and distances of candidate tiles

## Rules

- Read CLAUDE.md first for project conventions.
- Focus on the HIGHEST severity problem first.
- Read the failing code before writing any fix.
- **Evidence before fix**: if you can't identify root cause from existing logs,
  add diagnostic logging first and wait for the next cycle.
- Run `uv run pytest tests/` (skip tools/) — only commit if tests pass.
- Commit with a descriptive message explaining root cause and fix.
- Only modify files directly related to the fix.
- Do NOT remove existing logging — only add or modify.
"""
    print(f"  Calling Claude Code with report: {report_path}")
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=600,
            stdin=subprocess.DEVNULL,
        )
        output = result.stdout or ""
        if result.stderr:
            output += "\n--- stderr ---\n" + result.stderr
        return result.returncode == 0, output
    except FileNotFoundError as e:
        msg = f"Claude Code not found: {e}"
        print(f"  {msg}")
        return False, msg
    except subprocess.TimeoutExpired as e:
        msg = f"Claude Code timed out after 600s"
        partial = ""
        if e.stdout:
            partial = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else e.stdout
        print(f"  {msg}")
        return False, f"{msg}\n--- partial output ---\n{partial}"


def run_once(
    minutes: int = 10,
    call_claude_code: bool = False,
) -> None:
    """Run one analysis cycle."""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Analyzing last {minutes} min of logs...")

    data = parse_recent_log(minutes=minutes)
    if "error" in data:
        print(f"  Error: {data['error']}")
        return

    problems = detect_problems(data)
    report = generate_report(data, problems)
    path = save_report(report)

    # Print summary
    counts = data.get("counts", {})
    print(f"  Log: {data['total_lines']} total, {data['recent_lines']} recent lines")
    print(f"  Walk: {counts.get('walk_confirmed', 0)} OK / {counts.get('walk_denied', 0)} denied")
    print(f"  Skills: {data.get('skill_success_rate', 0):.0%} success rate")
    print(f"  Report: {path}")

    if problems:
        print(f"  Found {len(problems)} problems:")
        for p in problems:
            print(f"    [{p['severity']}] {p['name']}: {p['description']}")

        # Call Claude Code for HIGH/CRITICAL problems — it handles commit+push
        if call_claude_code and any(
            p["severity"] in ("HIGH", "CRITICAL") for p in problems
        ):
            success, output = call_claude(path)
            if output:
                print(f"  Claude Code output:\n{output[:500]}")
    else:
        print("  No problems detected.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Self-improvement analyzer")
    parser.add_argument(
        "--loop", action="store_true",
        help="Run continuously every N seconds (default 600)",
    )
    parser.add_argument(
        "--interval", type=int, default=600,
        help="Loop interval in seconds (default: 600 = 10min)",
    )
    parser.add_argument(
        "--minutes", type=int, default=10,
        help="How many minutes of log to analyze (default: 10)",
    )
    parser.add_argument(
        "--claude", action="store_true",
        help="Call Claude Code CLI for HIGH/CRITICAL problems",
    )
    args = parser.parse_args()

    if args.loop:
        print(f"Self-improvement loop (every {args.interval}s, {args.minutes}min window)")
        while True:
            run_once(minutes=args.minutes, call_claude_code=args.claude)
            time.sleep(args.interval)
    else:
        run_once(minutes=args.minutes, call_claude_code=args.claude)


if __name__ == "__main__":
    main()
