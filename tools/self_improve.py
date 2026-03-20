#!/usr/bin/env python3
"""Self-improvement loop — analyze logs, generate plan, optionally call Claude Code.

Usage:
    # One-shot analysis
    uv run python tools/self_improve.py

    # Continuous mode (run every 10 minutes)
    uv run python tools/self_improve.py --loop

    # Auto-apply safe fixes
    uv run python tools/self_improve.py --auto-fix

    # Call Claude Code for complex fixes
    uv run python tools/self_improve.py --claude
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Project root
ROOT = Path(__file__).parent.parent
LOG_FILE = ROOT / "data" / "anima.log"
ANALYSIS_DIR = ROOT / "data" / "analysis"
PLANS_DIR = ROOT / "data" / "plans"
TUNING_FILE = ROOT / "data" / "tuning.json"

# Tunable parameters and their ranges
TUNABLE_PARAMS = {
    "skills.chop_wood.search_radius": {
        "file": "anima/skills/gathering/lumber.py",
        "var": "SEARCH_RADIUS",
        "min": 3, "max": 15, "default": 8,
    },
    "skills.chop_wood.depleted_cooldown": {
        "file": "anima/skills/gathering/lumber.py",
        "var": "DEPLETED_COOLDOWN",
        "min": 300, "max": 3600, "default": 1200,
    },
    "brain.skill_cooldown": {
        "file": "anima/brain/brain.py",
        "var": "SKILL_COOLDOWN",
        "min": 0.1, "max": 5.0, "default": 0.5,
    },
    "movement.deny_cooldown": {
        "file": "anima/perception/walker.py",
        "var": "WALK_DELAY_MS",  # used in deny_walk
        "min": 100, "max": 1000, "default": 400,
    },
}


def parse_recent_log(minutes: int = 10) -> dict:
    """Parse recent log entries and extract metrics."""
    if not LOG_FILE.exists():
        return {"error": "No log file found"}

    counts: dict[str, int] = {
        "walk_confirmed": 0,
        "walk_denied": 0,
        "chop_success": 0,
        "chop_fail": 0,
        "chop_depleted": 0,
        "chop_unreachable": 0,
        "carpentry_success": 0,
        "carpentry_fail": 0,
        "carpentry_need_materials": 0,
        "skill_gain": 0,
        "stuck": 0,
        "escape_stuck": 0,
        "think_decided": 0,
        "speech_cliloc": 0,
    }

    recent_goals: list[str] = []
    recent_problems: list[str] = []
    positions: set[str] = set()

    with open(LOG_FILE) as f:
        for raw_line in f:
            # Strip ANSI color codes (log file may contain them)
            line = _ANSI_RE.sub("", raw_line)

            # Quick filter — only process recent lines
            if "2026-03-" not in line:
                continue

            for key in counts:
                if key in line:
                    counts[key] += 1
                    break

            if "goal_set" in line and "place=" in line:
                place = line.split("place=")[1].split()[0].strip("'")
                recent_goals.append(place)

            if "walk_denied" in line and "pos=" in line:
                pos = line.split("pos=")[1].split()[0].strip("'")
                positions.add(pos)

            if "stuck" in line.lower() or "unreachable" in line:
                recent_problems.append(line.strip()[-100:])

    return {
        "counts": counts,
        "unique_deny_positions": len(positions),
        "recent_goals": recent_goals[-5:],
        "recent_problems": recent_problems[-5:],
        "walk_success_rate": (
            counts["walk_confirmed"] / max(1, counts["walk_confirmed"] + counts["walk_denied"])
        ),
        "chop_success_rate": (
            counts["chop_success"] / max(1, counts["chop_success"] + counts["chop_fail"])
        ),
    }


def detect_problems(data: dict) -> list[dict]:
    """Detect problems from parsed log data."""
    problems = []
    counts = data.get("counts", {})

    if counts.get("walk_denied", 0) > 20 and data.get("unique_deny_positions", 0) < 3:
        problems.append({
            "severity": "HIGH",
            "name": "stuck_in_place",
            "description": (
                f"{counts['walk_denied']} denials, "
                f"{data['unique_deny_positions']} positions"
            ),
            "fix_type": "movement",
        })

    if counts.get("carpentry_need_materials", 0) > 5 and counts.get("chop_success", 0) == 0:
        problems.append({
            "severity": "MEDIUM",
            "name": "no_materials",
            "description": "Carpentry needs materials but no chopping success",
            "fix_type": "gathering",
        })

    if counts.get("walk_confirmed", 0) == 0 and counts.get("walk_denied", 0) > 0:
        problems.append({
            "severity": "CRITICAL",
            "name": "cannot_move",
            "description": "No successful walks — completely stuck",
            "fix_type": "movement",
        })

    if counts.get("think_decided", 0) > 10 and counts.get("walk_confirmed", 0) == 0:
        problems.append({
            "severity": "HIGH",
            "name": "thinking_not_acting",
            "description": "Many think decisions but no movement",
            "fix_type": "brain",
        })

    total_skill = counts.get("chop_success", 0) + counts.get("chop_fail", 0) + \
                  counts.get("carpentry_success", 0) + counts.get("carpentry_fail", 0)
    if total_skill > 20 and data.get("chop_success_rate", 0) < 0.1:
        problems.append({
            "severity": "MEDIUM",
            "name": "low_success_rate",
            "description": f"Skill success rate: {data['chop_success_rate']:.0%}",
            "fix_type": "parameter",
        })

    return problems


def generate_plan(data: dict, problems: list[dict]) -> str:
    """Generate an improvement plan as markdown."""
    ts = datetime.now()
    lines = [
        f"# Improvement Plan — {ts.strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Current State",
        "",
    ]

    counts = data.get("counts", {})
    wc = counts.get("walk_confirmed", 0)
    wd = counts.get("walk_denied", 0)
    lines.append(f"- Walk: {wc} OK / {wd} denied")
    cs = counts.get("chop_success", 0)
    cf = counts.get("chop_fail", 0)
    lines.append(f"- Chop: {cs} OK / {cf} fail")
    crs = counts.get("carpentry_success", 0)
    crf = counts.get("carpentry_fail", 0)
    lines.append(f"- Carpentry: {crs} OK / {crf} fail")
    lines.append(f"- Materials needed: {counts.get('carpentry_need_materials', 0)} times")
    lines.append(f"- Stuck events: {counts.get('stuck', 0)}")
    lines.append(f"- Goals: {', '.join(data.get('recent_goals', []))}")
    lines.append("")

    lines.append("## Problems")
    lines.append("")
    for p in problems:
        lines.append(f"- **[{p['severity']}]** {p['name']}: {p['description']}")
    if not problems:
        lines.append("No problems detected.")
    lines.append("")

    lines.append("## Suggested Actions")
    lines.append("")
    for p in problems:
        if p["fix_type"] == "movement":
            lines.append(f"- Fix movement: {p['description']}")
            lines.append("  - Check denied_tiles cache size")
            lines.append("  - Try escape_stuck with larger radius")
            lines.append("  - Consider teleporting to known safe location")
        elif p["fix_type"] == "gathering":
            lines.append(f"- Fix gathering: {p['description']}")
            lines.append("  - Increase SEARCH_RADIUS for trees")
            lines.append("  - Check weight limit")
            lines.append("  - Move to forest area")
        elif p["fix_type"] == "parameter":
            lines.append(f"- Tune parameters: {p['description']}")
            lines.append("  - Adjust SKILL_COOLDOWN")
            lines.append("  - Check can_execute conditions")
        elif p["fix_type"] == "brain":
            lines.append(f"- Fix brain: {p['description']}")
            lines.append("  - Check BT priority ordering")
            lines.append("  - Verify skill_exec vs think interaction")

    return "\n".join(lines) + "\n"


def save_plan(plan: str) -> Path:
    """Save plan to file."""
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    path = PLANS_DIR / f"{ts}_plan.md"
    path.write_text(plan)
    return path


def git_commit_and_push(message: str) -> bool:
    """Commit any changes and push."""
    try:
        # Check if there are changes
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        if not status.stdout.strip():
            return False  # nothing to commit

        subprocess.run(["git", "add", "-A"], cwd=str(ROOT), check=True)
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(ROOT), check=True,
        )
        subprocess.run(["git", "push"], cwd=str(ROOT), check=True)
        print(f"  Committed and pushed: {message}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"  Git error: {e}")
        return False


def call_claude(plan_path: Path) -> bool:
    """Call Claude Code CLI to apply the improvement plan."""
    prompt = f"""Read the improvement plan at {plan_path} and fix the issues described.

Rules:
- Only modify 1-2 files per fix
- Run uv run pytest after changes
- git commit with descriptive message
- git push after commit
- Focus on the highest severity problems first
- Don't change architecture, only fix specific issues
"""
    print(f"  Calling Claude Code with plan: {plan_path}")
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            cwd=str(ROOT),
            timeout=300,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"  Claude Code call failed: {e}")
        return False


def run_once(auto_fix: bool = False, call_claude_code: bool = False) -> None:
    """Run one analysis cycle."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Analyzing logs...")

    data = parse_recent_log(minutes=10)
    if "error" in data:
        print(f"  Error: {data['error']}")
        return

    problems = detect_problems(data)
    plan = generate_plan(data, problems)
    path = save_plan(plan)
    print(f"  Plan saved: {path}")

    if problems:
        print(f"  Found {len(problems)} problems:")
        for p in problems:
            print(f"    [{p['severity']}] {p['name']}: {p['description']}")
    else:
        print("  No problems detected.")

    if call_claude_code and any(p["severity"] in ("HIGH", "CRITICAL") for p in problems):
        call_claude(path)

    # Commit analysis/plans and any changes
    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    severity = max(
        (p["severity"] for p in problems),
        key=lambda s: ["LOW", "MEDIUM", "HIGH", "CRITICAL"].index(s),
        default="LOW",
    )
    git_commit_and_push(
        f"[auto-analyze] {ts_str} — {severity}, {len(problems)} issue(s)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Self-improvement analyzer")
    parser.add_argument("--loop", action="store_true", help="Run continuously every 10 minutes")
    parser.add_argument("--interval", type=int, default=600, help="Loop interval in seconds")
    parser.add_argument("--auto-fix", action="store_true", help="Auto-apply safe parameter fixes")
    parser.add_argument("--claude", action="store_true", help="Call Claude Code for complex fixes")
    args = parser.parse_args()

    if args.loop:
        print(f"Self-improvement loop started (every {args.interval}s)")
        while True:
            run_once(auto_fix=args.auto_fix, call_claude_code=args.claude)
            time.sleep(args.interval)
    else:
        run_once(auto_fix=args.auto_fix, call_claude_code=args.claude)


if __name__ == "__main__":
    main()
