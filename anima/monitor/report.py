"""Problem reporter — generates markdown reports when agent is stuck.

Creates timestamped .md files in data/reports/ with:
- Current situation (position, stats, weight)
- Inventory contents
- Recent activity log
- What was expected vs what happened
- Consecutive failures and context
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext

logger = structlog.get_logger()

REPORT_DIR = Path("data/reports")
# Don't report more than once per 10 minutes
REPORT_COOLDOWN = 600.0


async def report_problem(
    ctx: BrainContext,
    problem: str,
    expected: str = "",
    actual: str = "",
) -> str | None:
    """Generate a problem report as a markdown file.

    Args:
        ctx: Brain context with perception, blackboard, etc.
        problem: Short description of the problem.
        expected: What the agent expected to happen.
        actual: What actually happened.

    Returns:
        Path to the report file, or None if cooldown active.
    """
    import time

    now = time.time()
    last_report = ctx.blackboard.get("last_report_time", 0.0)
    if now - last_report < REPORT_COOLDOWN:
        return None
    ctx.blackboard["last_report_time"] = now

    ss = ctx.perception.self_state
    world = ctx.perception.world
    persona = ctx.blackboard.get("persona")
    agent_name = persona.name if persona else "Anima"

    ts = datetime.now()
    filename = f"{ts.strftime('%Y%m%d_%H%M%S')}_{agent_name}.md"

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = REPORT_DIR / filename

    # Gather data
    backpack = ss.equipment.get(0x15)
    inventory_lines = []
    if backpack:
        items = sorted(
            [it for it in world.items.values() if it.container == backpack],
            key=lambda it: it.name or "",
        )
        for it in items:
            name = it.name or f"0x{it.graphic:04X}"
            amt = f" x{it.amount}" if it.amount > 1 else ""
            inventory_lines.append(f"- {name}{amt}")

    equipment_lines = []
    for layer, eq_serial in ss.equipment.items():
        if layer == 0x15:
            continue  # skip backpack
        it = world.items.get(eq_serial)
        if it:
            name = it.name or f"0x{it.graphic:04X}"
            equipment_lines.append(f"- Layer 0x{layer:02X}: {name}")

    goal = ctx.blackboard.get("current_goal")
    goal_text = f"{goal['description']} → ({goal.get('x')}, {goal.get('y')})" if goal else "None"

    feed = ctx.blackboard.get("activity_feed")
    recent_activity = []
    if feed:
        for ev in feed.recent(10):
            evt = datetime.fromtimestamp(ev.timestamp).strftime("%H:%M:%S")
            recent_activity.append(f"- [{evt}] {ev.category}: {ev.message}")

    recent_journal = []
    for entry in ctx.perception.social.recent(count=10):
        jt = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M:%S")
        recent_journal.append(f"- [{jt}] {entry.name}: {entry.text[:80]}")

    skill_info = []
    for sk in sorted(ss.skills.values(), key=lambda s: -s.value):
        if sk.value > 0:
            lock_sym = {0: "↑", 1: "↓", 2: "•"}.get(sk.lock.value, "?")
            skill_info.append(f"- {sk.id}: {sk.value:.1f}/{sk.cap:.0f} {lock_sym}")

    consecutive_fails = ctx.blackboard.get("skill_consecutive_fails", 0)
    denied_count = len(ctx.walker.denied_tiles)

    # Build report
    report = f"""# Problem Report: {agent_name}

**Time**: {ts.strftime('%Y-%m-%d %H:%M:%S')}

## Problem

{problem}

## Expected vs Actual

- **Expected**: {expected or 'N/A'}
- **Actual**: {actual or 'N/A'}

## Agent State

| Field | Value |
|-------|-------|
| Position | ({ss.x}, {ss.y}, {ss.z}) |
| HP | {ss.hits}/{ss.hits_max} |
| Mana | {ss.mana}/{ss.mana_max} |
| Stamina | {ss.stam}/{ss.stam_max} |
| STR/DEX/INT | {ss.strength}/{ss.dexterity}/{ss.intelligence} |
| Weight | {ss.weight}/{ss.weight_max} |
| Gold | {ss.gold} |
| Current Goal | {goal_text} |
| Consecutive Skill Fails | {consecutive_fails} |
| Denied Tiles Cached | {denied_count} |

## Equipment

{chr(10).join(equipment_lines) if equipment_lines else 'None detected'}

## Inventory

{chr(10).join(inventory_lines) if inventory_lines else 'Empty'}

## Skills (non-zero)

{chr(10).join(skill_info) if skill_info else 'None'}

## Recent Activity

{chr(10).join(recent_activity) if recent_activity else 'None'}

## Recent Journal

{chr(10).join(recent_journal) if recent_journal else 'None'}
"""

    # Ask LLM to write the report if available
    llm = ctx.llm
    if llm:
        raw_data = report
        llm_prompt = f"""\
You are {agent_name}, writing a problem report about something that went wrong.
Write a brief, practical analysis in first person. Include:
1. What I was trying to do
2. What went wrong
3. Possible causes
4. What I should try next

Here is the raw situation data:
{raw_data}

Write the report concisely (under 300 words). Keep the data tables from above."""

        try:
            result = await llm.chat([
                {"role": "system", "content": f"You are {agent_name}, an AI in UO."},
                {"role": "user", "content": llm_prompt},
            ])
            if result.text:
                report = report + f"\n## Analysis (by {agent_name})\n\n{result.text}\n"
        except Exception:
            pass  # LLM unavailable — save raw report

    filepath.write_text(report)
    logger.info("problem_report_saved", path=str(filepath))

    act_feed = ctx.blackboard.get("activity_feed")
    if act_feed:
        act_feed.publish("system", f"Problem report saved: {filename}", importance=3)

    return str(filepath)
