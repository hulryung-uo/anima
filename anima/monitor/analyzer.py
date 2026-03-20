"""Log analyzer — detects problems and generates analysis reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from anima.monitor.metrics import WindowMetrics

ANALYSIS_DIR = Path("data/analysis")


@dataclass
class Problem:
    severity: str  # LOW, MEDIUM, HIGH, CRITICAL
    name: str
    description: str
    suggestion: str


def analyze(metrics: WindowMetrics) -> list[Problem]:
    """Detect problems from metrics."""
    problems: list[Problem] = []

    # Stuck loop
    if metrics.walk_denied > 15 and len(metrics.unique_positions) < 3:
        problems.append(Problem(
            severity="HIGH",
            name="stuck_loop",
            description=(
                f"{metrics.walk_denied} walk denials, "
                f"only {len(metrics.unique_positions)} unique positions"
            ),
            suggestion="Move to different area or improve pathfinding",
        ))

    # Walk failure rate
    if metrics.walk_success_rate < 0.5 and metrics.walk_confirmed + metrics.walk_denied > 10:
        problems.append(Problem(
            severity="MEDIUM",
            name="low_walk_success",
            description=f"Walk success rate {metrics.walk_success_rate:.0%}",
            suggestion="Check denied tile cache, Z-level issues, or dynamic obstacles",
        ))

    # Skill spam (executing but never succeeding)
    if metrics.skill_fail > 10 and metrics.skill_success == 0:
        problems.append(Problem(
            severity="HIGH",
            name="skill_all_fail",
            description=f"{metrics.skill_fail} skill failures, 0 successes",
            suggestion="Check can_execute conditions, materials, tools",
        ))

    # No progress
    total_activity = (
        metrics.walk_confirmed + metrics.skill_success
        + metrics.chop_success + metrics.craft_success
    )
    if total_activity == 0 and metrics.walk_denied + metrics.skill_fail > 5:
        problems.append(Problem(
            severity="CRITICAL",
            name="no_progress",
            description="No successful actions in the last window",
            suggestion="Agent is completely stuck — needs intervention",
        ))

    # Weight blocked
    if metrics.skill_fail > 5 and metrics.chop_success == 0 and metrics.craft_success == 0:
        problems.append(Problem(
            severity="MEDIUM",
            name="possibly_overweight",
            description="No gathering or crafting success — may be overweight",
            suggestion="Go sell items or drop heavy items",
        ))

    # Good progress (no problems)
    if not problems and metrics.skill_success > 0:
        problems.append(Problem(
            severity="LOW",
            name="healthy",
            description="Agent is operating normally",
            suggestion="No action needed",
        ))

    return problems


def generate_report(
    metrics: WindowMetrics,
    problems: list[Problem],
    agent_name: str = "Anima",
) -> str:
    """Generate markdown analysis report."""
    ts = datetime.now()

    lines = [
        f"# Analysis Report — {ts.strftime('%Y-%m-%d %H:%M')}",
        f"\nAgent: **{agent_name}**",
        f"\n## Metrics (last {metrics.window_seconds / 60:.0f} minutes)\n",
        "| Metric | Value | Status |",
        "|--------|-------|--------|",
    ]

    def _status(val: float, good: float, bad: float) -> str:
        if val >= good:
            return "OK"
        if val >= bad:
            return "WARN"
        return "BAD"

    lines.append(
        f"| Walk success rate | {metrics.walk_success_rate:.0%} "
        f"({metrics.walk_confirmed}/{metrics.walk_confirmed + metrics.walk_denied}) "
        f"| {_status(metrics.walk_success_rate, 0.8, 0.5)} |"
    )
    lines.append(
        f"| Skill success rate | {metrics.skill_success_rate:.0%} "
        f"({metrics.skill_success}/{metrics.skill_success + metrics.skill_fail}) "
        f"| {_status(metrics.skill_success_rate, 0.3, 0.1)} |"
    )
    lines.append(
        f"| Chop success rate | {metrics.chop_success_rate:.0%} "
        f"({metrics.chop_success}/"
        f"{metrics.chop_success + metrics.chop_fail + metrics.chop_depleted}) "
        f"| {_status(metrics.chop_success_rate, 0.2, 0.05)} |"
    )
    lines.append(f"| Distance moved | {metrics.distance_moved} tiles | |")
    lines.append(f"| Unique positions | {len(metrics.unique_positions)} | |")
    lines.append(f"| Stuck events | {metrics.stuck_count} | |")
    lines.append(f"| Gold earned | {metrics.gold_earned} | |")
    lines.append(
        f"| Skill gains | {len(metrics.skill_gains)} | |"
    )

    lines.append("\n## Problems Detected\n")
    if problems:
        for i, p in enumerate(problems, 1):
            lines.append(f"{i}. **[{p.severity}]** {p.name}: {p.description}")
            lines.append(f"   - Suggestion: {p.suggestion}")
    else:
        lines.append("No problems detected.")

    lines.append("\n## Raw Counts\n")
    lines.append(f"- Walk: {metrics.walk_confirmed} confirmed, {metrics.walk_denied} denied")
    chop_total = f"{metrics.chop_success}s/{metrics.chop_fail}f/{metrics.chop_depleted}d"
    lines.append(f"- Chop: {chop_total}")
    lines.append(f"- Craft: {metrics.craft_success} success, {metrics.craft_fail} fail")
    lines.append(f"- Gold: +{metrics.gold_earned} / -{metrics.gold_spent}")

    return "\n".join(lines) + "\n"


def save_report(report: str, agent_name: str = "Anima") -> Path:
    """Save analysis report to file."""
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    path = ANALYSIS_DIR / f"{ts}_{agent_name}.md"
    path.write_text(report)
    return path
