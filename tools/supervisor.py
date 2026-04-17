#!/usr/bin/env python3
"""Supervisor — run agent + 3-level self-improvement.

Level 1: Automatic recovery (no Claude Code)
  - Agent crash → restart
  - Agent stuck (no activity 5min) → restart with fresh state
  - Agent idle (no progress 10min) → clear blackboard, restart

Level 2: Targeted diagnosis (Claude Code, focused prompt)
  - Query action_logs DB for failure patterns
  - Send specific procedure + error to Claude Code
  - Small, focused fixes only

Level 3: Full analysis (Claude Code, comprehensive)
  - Periodic full log + DB analysis
  - Broad prompt for structural issues

Usage:
    uv run python tools/supervisor.py
    uv run python tools/supervisor.py --no-claude
    uv run python tools/supervisor.py --interval 600
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# tools/ must be on sys.path before importing helper modules
_TOOLS_DIR = str(Path(__file__).parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fix_lock import FixLock  # noqa: E402
from fix_tier import call_quick_fix, is_too_complex  # noqa: E402

ROOT = Path(__file__).parent.parent
AGENT_CMD = [sys.executable, "-m", "anima"]
STATE_FILE = ROOT / "data" / "state.json"
DB_FILE = ROOT / "data" / "anima.db"
CLAUDE_LOG = ROOT / "data" / "claude_code.log"
IMPROVEMENTS_LOG = ROOT / "data" / "improvements.jsonl"
HINTS_FILE = ROOT / "data" / "supervisor_hints.json"

# Output split:
# - Status updates (agent intent / pos / weight / gold / procedure) go to stdio.
# - Operational noise (analysis reports, auto-push/commit, quick-fix attempts)
#   goes to SUPERVISOR_LOG.
# - The agent's own console output (structlog INFO/DEBUG lines) is verbose
#   enough to drown the status feed, so we redirect it to AGENT_STDIO_LOG.
#   The agent also writes structured logs to data/anima.log via its own
#   handler — AGENT_STDIO_LOG only catches stray prints / tracebacks.
SUPERVISOR_LOG = ROOT / "data" / "supervisor.log"
AGENT_STDIO_LOG = ROOT / "data" / "agent_stdio.log"

WARMUP_SECONDS = 90
STUCK_THRESHOLD = 300      # 5 minutes no activity → stuck
IDLE_THRESHOLD = 600       # 10 minutes no progress → idle
MAX_RESTARTS_PER_HOUR = 4  # prevent restart loop — too fast kills server sessions

_last_status_key: tuple | None = None
_last_event_ts: float = 0.0

# Procedures whose action.end events are surfaced on stdio so the user
# sees buy/sell/craft outcomes (success message + reason) live, not just
# next-step intent. Add to this set if a procedure's outcome is "newsworthy".
_NOTABLE_EVENT_PROCEDURES = {
    "buy_from_vendor",
    "sell_to_vendor",
    "craft_blacksmith",
    "make_tools",
    "smelt_ore",
    "bank_deposit",
}

# Topics whose every event surfaces on stdio regardless of action.end
# parsing — used for explicit planner-level signals (e.g. "no progress
# path, user intervention required") and strategy decisions.
_NOTABLE_TOPICS = {
    "planner.stopped",
    "planner.death",
    "planner.critical_hp",
    "metrics.hourly_complete",
    "metrics.alert",
}


def _debug(msg: str) -> None:
    """Append one operational event to data/supervisor.log."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        SUPERVISOR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SUPERVISOR_LOG, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _alert(msg: str) -> None:
    """Operational event important enough to surface on stdio (crashes, recoveries)."""
    _debug(msg)
    print(f"[supervisor] {msg}", flush=True)


def _print_status() -> None:
    """Print a compact one-line status snapshot + any new notable events.

    Status (pos / hp / weight / gold / proc / intent) prints only when it
    changes. Notable procedure outcomes (buy/sell/craft) print every time a
    new action.end event for them shows up in state.json's activity feed,
    so stdio surfaces "샀나 못샀나, 왜 못샀나" live.
    """
    global _last_status_key, _last_event_ts
    if not STATE_FILE.exists():
        return
    try:
        s = json.loads(STATE_FILE.read_text())
    except Exception:
        return

    # --- Notable events first (so they precede the status line) ---
    for entry in s.get("activity", []):
        ts_e = entry.get("ts", 0)
        if ts_e <= _last_event_ts:
            continue
        topic = entry.get("topic", "")
        msg = entry.get("message") or ""
        if topic in _NOTABLE_TOPICS:
            t = time.strftime("%H:%M:%S", time.localtime(ts_e))
            print(f"[{t}] EVENT: {msg}", flush=True)
        elif topic == "action.end":
            # Format: "✓ <proc>: <result-msg>" or "✗ <proc>: <fail-msg>"
            proc_name = ""
            if ":" in msg:
                head = msg.split(":", 1)[0].strip()
                proc_name = head.lstrip("✓✗ \t")
            if proc_name in _NOTABLE_EVENT_PROCEDURES:
                t = time.strftime("%H:%M:%S", time.localtime(ts_e))
                print(f"[{t}] EVENT: {msg}", flush=True)
        _last_event_ts = max(_last_event_ts, ts_e)

    # --- Status line (de-duped) ---
    p = s.get("status") or {}
    if not p:
        return
    intent = (p.get("intent") or "").strip()
    proc = (p.get("procedure") or "").strip() or "-"
    hp_cur = p.get("hp", 0)
    hp_max = p.get("hp_max", 1)
    key = (
        p.get("x"), p.get("y"),
        hp_cur, p.get("weight"), p.get("gold"),
        proc, intent,
    )
    if key == _last_status_key:
        return
    _last_status_key = key

    ts = time.strftime("%H:%M:%S")
    pos = f"({p.get('x','?')},{p.get('y','?')})"
    w = f"{p.get('weight','?')}/{p.get('weight_max','?')}"
    g = p.get('gold', '?')
    intent_short = intent[:60]

    # HP alert prefix
    if hp_cur <= 0 and hp_max > 0:
        hp_str = f"DEAD(0/{hp_max})"
        prefix = "☠ "
    elif hp_max > 0 and hp_cur < hp_max * 0.3:
        hp_str = f"LOW({hp_cur}/{hp_max})"
        prefix = "⚠ "
    else:
        hp_str = f"{hp_cur}/{hp_max}"
        prefix = ""

    print(
        f"{prefix}[{ts}] pos={pos} hp={hp_str} w={w} g={g} proc={proc} intent='{intent_short}'",
        flush=True,
    )


def get_git_head() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def start_agent(extra_args: list[str] | None = None) -> subprocess.Popen:
    cmd = AGENT_CMD + (extra_args or [])
    _alert(f"Starting agent: {' '.join(cmd)}")
    # Route agent stdout/stderr to a dedicated log so the supervisor's
    # status feed stays readable. The agent writes its own structured log
    # to data/anima.log; this catches stray prints and tracebacks only.
    AGENT_STDIO_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(AGENT_STDIO_LOG, "ab", buffering=0)
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log_fp, stderr=log_fp)
    _alert(f"Agent PID {proc.pid} (stdio → {AGENT_STDIO_LOG})")
    return proc


def stop_agent(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    _alert(f"Stopping agent PID {proc.pid}...")
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# Level 1: Automatic recovery
# ---------------------------------------------------------------------------

def read_agent_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


def _check_action_log_loops() -> str | None:
    """Check action_logs for stuck loop patterns (last 8 minutes).

    Raised from 5 to 8 min after the expedition-cycle redesign:
    COLLECTING tours legitimately walk 30+ tiles to remembered piles,
    and a single `pick_up_ore_and_smelt` can exceed 5 min without
    completing. 8 min stays aggressive enough to catch real stalls
    while tolerating normal tour durations.
    """
    if not DB_FILE.exists():
        return None
    try:
        conn = sqlite3.connect(str(DB_FILE))
        cutoff = time.time() - 480  # last 8 min

        rows = conn.execute("""
            SELECT procedure, result, COUNT(*) as cnt
            FROM action_logs WHERE timestamp > ?
            GROUP BY procedure, result ORDER BY cnt DESC
        """, (cutoff,)).fetchall()

        proc_stats: dict[str, dict[str, int]] = {}
        for proc, result, cnt in rows:
            if proc not in proc_stats:
                proc_stats[proc] = {"success": 0, "fail": 0}
            if result == "success":
                proc_stats[proc]["success"] += cnt
            else:
                proc_stats[proc]["fail"] += cnt

        for proc, stats in proc_stats.items():
            total = stats["success"] + stats["fail"]
            if total >= 20 and stats["success"] == 0:
                conn.close()
                return f"stuck loop: {proc} failed {stats['fail']}x with 0 success in 5min"

        total_actions = sum(s["success"] + s["fail"] for s in proc_stats.values())
        if total_actions == 0:
            if STATE_FILE.exists():
                try:
                    st = json.loads(STATE_FILE.read_text())
                    if time.time() - st.get("ts", 0) < 30:
                        conn.close()
                        return "agent alive but zero procedures in 8min (planner idle loop)"
                except Exception:
                    pass

        conn.close()
    except Exception:
        pass
    return None


def check_agent_health(state: dict | None) -> str | None:
    """Returns a problem description if agent is unhealthy, None if OK."""
    if state is None:
        return None  # no state yet

    now = time.time()
    state_ts = state.get("ts", 0)

    # state.json too old → agent not updating
    if now - state_ts > 120:
        return f"state.json is {now - state_ts:.0f}s stale"

    # No activity for STUCK_THRESHOLD
    activity = state.get("activity", [])
    if activity:
        last_act_ts = activity[-1].get("ts", 0)
        if now - last_act_ts > STUCK_THRESHOLD:
            last_msg = activity[-1].get("message", "")
            return f"no activity for {now - last_act_ts:.0f}s (last: {last_msg})"

    # Check for deadlock state from agent intent
    status = state.get("status", {})
    intent = status.get("intent", "")
    if "교착 상태" in intent or "DEADLOCK" in intent:
        return f"agent reports deadlock: {intent[:80]}"

    # Check for stuck loop via action_logs — same procedure failing repeatedly
    stuck_info = _check_action_log_loops()
    if stuck_info:
        return stuck_info

    return None


def auto_recover(proc: subprocess.Popen, reason: str, extra_args: list[str]) -> subprocess.Popen:
    """Level 1: Stop agent, clear transient state, restart."""
    _alert(f"AUTO-RECOVER: {reason}")

    stop_agent(proc)

    # Record supervisor-side auto-recover as a metric event so the
    # agent's pipeline sees stuck_events regardless of whether the
    # new agent instance has started yet.
    try:
        events_file = ROOT / "data" / "metrics_events.jsonl"
        events_file.parent.mkdir(parents=True, exist_ok=True)
        with open(events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "type": "stuck_event",
                "reason": reason,
                "source": "supervisor",
            }) + "\n")
    except Exception:
        pass

    # Log the recovery
    _log_improvement("auto_recover", reason, success=True, code_changed=False)

    time.sleep(3)
    return start_agent(extra_args)


# ---------------------------------------------------------------------------
# Level 2: Targeted diagnosis
# ---------------------------------------------------------------------------

def get_top_failures(minutes: int = 30) -> list[dict]:
    """Query action_logs DB for procedures with high failure rates."""
    if not DB_FILE.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_FILE))
        cutoff = time.time() - minutes * 60
        rows = conn.execute("""
            SELECT procedure, result, COUNT(*) as cnt
            FROM action_logs WHERE timestamp > ?
            GROUP BY procedure, result ORDER BY cnt DESC
        """, (cutoff,)).fetchall()
        conn.close()

        # Find procedures that fail much more than succeed
        stats: dict[str, dict] = {}
        for proc, result, cnt in rows:
            if proc not in stats:
                stats[proc] = {"success": 0, "fail": 0, "fail_types": {}}
            if result == "success":
                stats[proc]["success"] += cnt
            else:
                stats[proc]["fail"] += cnt
                stats[proc]["fail_types"][result] = cnt

        problems = []
        for proc, s in stats.items():
            total = s["success"] + s["fail"]
            if total < 3:
                continue
            fail_rate = s["fail"] / total
            if fail_rate > 0.8:
                top_fail = max(s["fail_types"].items(), key=lambda x: x[1])
                problems.append({
                    "procedure": proc,
                    "fail_rate": fail_rate,
                    "total": total,
                    "top_failure": top_fail[0],
                    "top_count": top_fail[1],
                })
        return sorted(problems, key=lambda x: -x["total"])
    except Exception:
        return []


def build_diagnostic_prompt(failure: dict | None = None, minutes: int = 10) -> str:
    """Build a rich diagnostic prompt with full context from all sources."""
    from diagnose import collect, render_prompt

    diag = collect(minutes=minutes)
    context = render_prompt(diag)

    # Check for deadlock — code fix won't help
    constraints = diag.get("constraints", {})
    if constraints.get("DEADLOCK"):
        return f"""The Anima UO AI agent is in a TRUE DEADLOCK state.

{context}

This is NOT a code bug. The agent has no tools, no gold, and no materials.
No procedure can start because all preconditions fail.

Your task: Add a recovery strategy to the planner for this situation.
Options to consider:
1. Walk around nearby area looking for items on the ground to pick up
2. Find a monster to kill for gold loot (if agent has combat capability)
3. Post to forum asking for help (already implemented in _escalate_to_forum)
4. Walk to a populated area where other players/agents might help
5. Try to find a different type of resource (wood instead of ore)

Read CLAUDE.md first. Read anima/planner/planner.py _resolve_deadlock().
Add a concrete recovery path, not just logging.
Run `uv run pytest` — only commit if tests pass.
`git commit` with descriptive message.
"""

    # Normal diagnostic prompt
    problem_summary = ""
    if failure:
        proc = failure["procedure"]
        fail_type = failure["top_failure"]
        fail_rate = failure["fail_rate"]
        total = failure["total"]
        problem_summary = f"""
## Primary Problem
Procedure `{proc}` fails {fail_rate:.0%} ({total} attempts).
Most common failure: `{fail_type}`
"""

    return f"""You are debugging the Anima UO AI agent. Below is rich diagnostic data
from the last {minutes} minutes, including procedure stats, server messages,
planner decisions, and constraint analysis.

{problem_summary}

{context}

## Your debugging method

1. **Read the diagnostic data above carefully** — the answer is usually in the
   failure messages, server responses, or constraint list.

2. **Identify the pattern:**
   - Same procedure failing repeatedly with same message → stuck loop
   - "insufficient metal" but ingots counted > needed → hue mismatch (iron vs colored)
   - "too far away" → tile search returning tiles outside action range
   - Vendor "not interested" → wrong vendor type for the items being sold
   - "gump did not open" → tool double-click failed or NPC not nearby
   - 0 procedures selected → all can_start() return false → check constraints
   - Planner returning None every tick → check fall-through in select_procedure

3. **Check past fix attempts** (listed above) — don't retry what already failed.

4. **If root cause is unclear from this data**, add diagnostic logging first:
   - Log the specific values being checked (e.g., item.hue, vendor.name)
   - Log can_start() result with reason
   - Don't guess at fixes without evidence

## Architecture
- Planner: anima/planner/planner.py — priority 1-9 procedure selection
- Procedures: anima/procedures/ — mine_ore, smelt_ore, craft_blacksmith, sell/buy_from_vendor, bank_deposit, make_tools
- Vendor: anima/skills/trade/vendor.py — _find_vendor, context menu
- Movement: anima/action/movement.py — go_to() pathfinding
- Gump: anima/actions/gump.py — craft menu interaction

## UO Domain Knowledge
- Ingots have hue: 0=iron (default), non-zero=colored (gold, valorite, etc.)
  → count_items without hue filter includes ALL ingots, server only accepts matching type
- Vendors only buy items in their SBInfo list (weaponsmith ≠ tanner)
- NPC names arrive via OPL packets, not in MobileIncoming — must request explicitly
- Mining range is 2 tiles; _find_mineable_tile searches up to 8 → can_start must re-check distance
- Gump responses need switches[] and text_entries[] or server silently rejects
- After crafting, read gump notice BEFORE calling _close_all_gumps() or data is lost

## Rules
- Read CLAUDE.md first for project conventions
- Focus on the highest severity problem
- Read the failing code before writing any fix
- Run `uv run pytest tests/` (skip tools/) — only commit if tests pass
- `git commit` with descriptive message
- If problem is already fixed in code but agent hasn't restarted, just note it
"""



def call_claude_with_prompt(prompt: str, timeout: int = 300) -> tuple[bool, str]:
    """Call Claude Code CLI with a prompt. Returns (success, output)."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        output = result.stdout or ""
        if result.stderr:
            output += "\n--- stderr ---\n" + result.stderr

        # Log to claude_code.log
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(CLAUDE_LOG, "a") as f:
            f.write(f"\n{'='*72}\n[{ts}]\n{'='*72}\n")
            f.write(output[:2000])
            f.write("\n")

        return result.returncode == 0, output
    except subprocess.TimeoutExpired as e:
        partial: str = ""
        if e.stdout:
            partial = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else str(e.stdout)
        if e.stderr:
            stderr_text = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else str(e.stderr)
            partial = partial + "\n--- stderr ---\n" + stderr_text

        # Log partial output — Claude may have committed before timeout
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(CLAUDE_LOG, "a") as f:
            f.write(f"\n{'='*72}\n[{ts}] TIMED OUT after {timeout}s\n{'='*72}\n")
            f.write((partial or "(no output captured)")[:2000])
            f.write("\n")

        return False, f"Claude Code timed out after {timeout}s\n{partial[:500]}"
    except FileNotFoundError:
        return False, "Claude Code CLI not found"


# ---------------------------------------------------------------------------
# Level 3: Full analysis (existing self_improve.py)
# ---------------------------------------------------------------------------

def run_full_analysis(minutes: int) -> tuple[list[dict], str | None]:
    """Run comprehensive log + DB analysis."""
    from self_improve import detect_problems, generate_report, parse_recent_log, save_report

    _debug(f"Full analysis (last {minutes} min)...")

    data = parse_recent_log(minutes=minutes)
    if "error" in data:
        _debug(f"Analysis error: {data['error']}")
        return [], None

    problems = detect_problems(data)
    report = generate_report(data, problems)
    path = save_report(report)

    _debug(f"Report: {path}")
    if problems:
        for p in problems:
            _debug(f"  [{p['severity']}] {p['name']}: {p['description']}")
    else:
        _debug("No problems detected.")

    return problems, str(path) if path else None


# ---------------------------------------------------------------------------
# Improvement log
# ---------------------------------------------------------------------------

def _log_improvement(action: str, reason: str, success: bool,
                     code_changed: bool, output_preview: str = "") -> None:
    IMPROVEMENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "reason": reason,
        "success": success,
        "code_changed": code_changed,
        "commit": get_git_head()[:8],
        "output_preview": output_preview[:300],
    }
    with open(IMPROVEMENTS_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _load_fix_attempts() -> dict[str, int]:
    """Load fix attempt counts from improvements.jsonl.

    Counts consecutive failed targeted_fix entries per fix_key.
    Resets count when a successful targeted_fix is found for the same key.
    """
    if not IMPROVEMENTS_LOG.exists():
        return {}
    attempts: dict[str, int] = {}
    try:
        with open(IMPROVEMENTS_LOG) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                action = entry.get("action", "")
                if not action.startswith("targeted_fix:"):
                    continue
                # Extract fix_key: "procedure:reason"
                proc = action.split(":", 1)[1]  # e.g. "craft_blacksmith"
                reason_raw = entry.get("reason", "")
                # reason format: "missing_resource (fail rate 100%)"
                reason = reason_raw.split(" (")[0] if " (" in reason_raw else reason_raw
                fix_key = f"{proc}:{reason}"

                if entry.get("success") or entry.get("code_changed"):
                    attempts[fix_key] = 0
                else:
                    attempts[fix_key] = attempts.get(fix_key, 0) + 1
    except Exception:
        pass
    return {k: v for k, v in attempts.items() if v > 0}


def _get_timeout(attempt: int) -> int:
    """Progressive timeout: 300s -> 450s -> 600s."""
    timeouts = [400, 600, 900]
    return timeouts[min(attempt, len(timeouts) - 1)]


def _auto_push_if_ahead() -> None:
    """Push to origin if local HEAD is ahead of upstream.

    Best-effort: network failures, conflicts, or missing upstream never raise.
    Runs unconditionally after every fix cycle, so it catches both:
      - commits just made by _auto_commit_if_needed()
      - commits Claude Code made itself but left unpushed (the common case)
    """
    try:
        ahead = subprocess.run(
            ["git", "rev-list", "--count", "@{u}..HEAD"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        if ahead.returncode != 0:
            # No upstream configured or other error — skip quietly
            return
        count = ahead.stdout.strip()
        if not count or count == "0":
            return

        push = subprocess.run(
            ["git", "push", "origin", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True,
            timeout=30,
        )
        if push.returncode == 0:
            _debug(f"Auto-pushed {count} commit(s) to origin")
        else:
            _debug(f"Auto-push failed (commits kept locally): {push.stderr[:200]}")
    except subprocess.TimeoutExpired:
        _debug("Auto-push timed out (commits kept locally)")
    except Exception as e:
        _debug(f"Auto-push error (commits kept locally): {e}")


def _auto_commit_if_needed() -> bool:
    """Commit uncommitted changes left by Claude Code.

    Returns True if a commit was made. Does NOT push — the caller should
    invoke _auto_push_if_ahead() afterwards so that prior unpushed commits
    are also caught up.
    """
    try:
        status = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        if not status.stdout.strip():
            return False  # nothing to commit

        # Stage and commit
        subprocess.run(["git", "add", "-A"], cwd=str(ROOT))
        result = subprocess.run(
            ["git", "commit", "-m",
             "Auto-commit: Claude Code changes (tests passed)"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        if result.returncode != 0:
            _debug(f"Auto-commit failed: {result.stderr[:200]}")
            return False

        _debug("Auto-committed uncommitted Claude Code changes")
        return True
    except Exception as e:
        _debug(f"Auto-commit error: {e}")
        return False


def _write_skip_hint(procedure: str, reason: str, ttl_hours: float = 1.0) -> None:
    """Write a skip hint for the planner to read."""
    hints: dict = {}
    if HINTS_FILE.exists():
        try:
            hints = json.loads(HINTS_FILE.read_text())
        except Exception:
            pass
    skip = hints.setdefault("skip_procedures", {})
    skip[procedure] = {
        "until": time.time() + ttl_hours * 3600,
        "reason": reason,
    }
    HINTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HINTS_FILE.write_text(json.dumps(hints, indent=2))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Anima supervisor — 3-level self-improvement")
    parser.add_argument("--interval", type=int, default=600, help="Analysis interval (seconds)")
    parser.add_argument("--minutes", type=int, default=10, help="Minutes of log to analyze")
    parser.add_argument("--no-claude", action="store_true", help="Level 1 only — no Claude Code")
    parser.add_argument("--agent-args", nargs="*", default=[], help="Extra args for agent")
    args = parser.parse_args()

    use_claude = not args.no_claude
    mode = "Level 1-3" if use_claude else "Level 1 only"
    _alert(f"Starting ({mode}, interval={args.interval}s); debug log → {SUPERVISOR_LOG}")

    agent_proc = start_agent(args.agent_args)
    last_analysis = time.time()
    last_health_check = time.time()
    last_git_head = get_git_head()
    restarts_this_hour: list[float] = []
    consecutive_recoveries = 0  # backoff counter for rapid auto_recover
    cycle = 0
    # Load fix attempts from disk (persists across restarts)
    fix_attempts: dict[str, int] = _load_fix_attempts()
    MAX_FIX_ATTEMPTS = 3

    try:
        while True:
            now = time.time()

            # Stream agent state to stdio (only when meaningfully changed)
            _print_status()

            # Check if agent process is alive
            if agent_proc.poll() is not None:
                _alert(f"Agent exited (code {agent_proc.returncode})")
                time.sleep(10)
                agent_proc = start_agent(args.agent_args)
                last_analysis = now
                continue

            # --- Level 1: Health check every 30s ---
            if now - last_health_check >= 30:
                last_health_check = now

                # Check if code changed (git commit) → restart agent with new code
                # If tools/ changed, re-exec supervisor itself
                current_head = get_git_head()
                if current_head and current_head != last_git_head:
                    # Check if supervisor/tools code changed
                    try:
                        changed = subprocess.run(
                            ["git", "diff", "--name-only",
                             last_git_head, current_head],
                            cwd=str(ROOT), capture_output=True, text=True,
                        ).stdout
                        tools_changed = any(
                            f.startswith("tools/") for f in changed.splitlines()
                        )
                    except Exception:
                        tools_changed = False

                    if tools_changed:
                        _alert(f"tools/ changed ({last_git_head[:8]} → {current_head[:8]}) — restarting supervisor")
                        stop_agent(agent_proc)
                        os.execv(sys.executable, [sys.executable] + sys.argv)

                    _alert(f"Code changed ({last_git_head[:8]} → {current_head[:8]}) — restarting agent")
                    last_git_head = current_head
                    consecutive_recoveries = 0
                    stop_agent(agent_proc)
                    agent_proc = start_agent(args.agent_args)
                    last_analysis = now
                    continue

                state = read_agent_state()
                problem = check_agent_health(state)

                if problem:
                    # Rate-limit restarts
                    restarts_this_hour = [t for t in restarts_this_hour if now - t < 3600]
                    if len(restarts_this_hour) < MAX_RESTARTS_PER_HOUR:
                        # Backoff: wait longer between consecutive recoveries
                        # (caps at 180s so supervisor doesn't hang indefinitely)
                        if consecutive_recoveries > 0:
                            wait = min(consecutive_recoveries * 60, 180)
                            _debug(f"Backoff: waiting {wait}s before recovery #{consecutive_recoveries + 1}")
                            time.sleep(wait)
                        restarts_this_hour.append(now)
                        consecutive_recoveries += 1
                        agent_proc = auto_recover(agent_proc, problem, args.agent_args)
                        last_analysis = now
                        continue
                    else:
                        _alert(f"⚠ Too many restarts ({len(restarts_this_hour)}/hr), skipping")
                else:
                    consecutive_recoveries = 0  # reset on healthy check

            # --- Level 2 & 3: Periodic analysis ---
            wait_time = WARMUP_SECONDS if cycle == 0 else args.interval
            if now - last_analysis < wait_time:
                time.sleep(5)
                continue

            last_analysis = now
            cycle += 1

            if use_claude:
                # Level 2: Check DB for specific procedure failures
                failures = get_top_failures(minutes=args.minutes)
                if failures:
                    worst = failures[0]
                    fix_key = f"{worst['procedure']}:{worst['top_failure']}"
                    attempts = fix_attempts.get(fix_key, 0)

                    if worst["fail_rate"] > 0.8 and worst["total"] > 10:
                        if attempts >= MAX_FIX_ATTEMPTS:
                            _debug(f"Skipping {fix_key} — already failed {attempts} fix attempts")
                            _log_improvement(
                                f"skip:{worst['procedure']}",
                                f"gave up after {attempts} failed fix attempts for {worst['top_failure']}",
                                success=False, code_changed=False,
                            )
                            _write_skip_hint(worst["procedure"], worst["top_failure"])
                        else:
                            proc_name = worst["procedure"]
                            timeout = _get_timeout(attempts)
                            _debug(f"TARGETED FIX: {proc_name} ({worst['top_failure']}) attempt={attempts + 1} timeout={timeout}s")
                            stop_agent(agent_proc)
                            prompt = build_diagnostic_prompt(failure=worst)
                            head_before = get_git_head()
                            lock_key = f"targeted_fix:{fix_key}"
                            with FixLock(lock_key, sha=head_before) as got:
                                if not got:
                                    _debug(f"{lock_key} already being fixed — skipping")
                                    agent_proc = start_agent(args.agent_args)
                                    last_analysis = time.time()
                                    continue
                                # --- NEW: try Haiku quick fix first ---
                                quick_diag = prompt[:2000]  # truncate for Haiku
                                q_success, q_committed, q_output = call_quick_fix(
                                    quick_diag, fix_key, timeout=60,
                                )
                                if q_committed and q_success:
                                    _debug(f"Quick fix committed for {fix_key}")
                                    success, output = True, q_output
                                elif q_success and not is_too_complex(q_output):
                                    # Haiku ran cleanly but didn't commit; escalate
                                    _debug("Quick fix ran without changes — escalating to Opus")
                                    success, output = call_claude_with_prompt(prompt, timeout=timeout)
                                else:
                                    # Too complex or failed → Opus
                                    _debug("Quick fix says too complex — escalating to Opus")
                                    success, output = call_claude_with_prompt(prompt, timeout=timeout)
                            # Auto-commit if Claude left uncommitted changes
                            if _auto_commit_if_needed():
                                pass  # committed
                            # Catch up any unpushed commits (Claude's or ours)
                            _auto_push_if_ahead()
                            head_after = get_git_head()
                            code_changed = head_before != head_after and head_after != ""
                            _log_improvement(
                                f"targeted_fix:{proc_name}",
                                f"{worst['top_failure']} (fail rate {worst['fail_rate']:.0%})",
                                success=success,
                                code_changed=code_changed,
                                output_preview=output[:500],
                            )
                            if code_changed:
                                _debug(f"Targeted fix committed for {proc_name}")
                                fix_attempts[fix_key] = 0
                            elif success:
                                _debug(f"Claude ran but no changes for {proc_name}")
                                fix_attempts[fix_key] = attempts + 1
                            else:
                                _debug(f"Claude failed for {proc_name}")
                                fix_attempts[fix_key] = attempts + 1
                            agent_proc = start_agent(args.agent_args)
                            last_analysis = time.time()
                            continue

                # Level 3: Full analysis — use rich diagnostic prompt
                problems, report_path = run_full_analysis(args.minutes)
                if problems and report_path:
                    severe = [p for p in problems if p["severity"] in ("HIGH", "CRITICAL")]
                    if severe:
                        stop_agent(agent_proc)
                        prompt = build_diagnostic_prompt(minutes=args.minutes)
                        head_before = get_git_head()
                        lock_key = f"full_analysis:{severe[0]['name']}"
                        with FixLock(lock_key, sha=head_before) as got:
                            if not got:
                                _debug(f"{lock_key} already being fixed — skipping")
                                agent_proc = start_agent(args.agent_args)
                                last_analysis = time.time()
                                continue
                            _, output = call_claude_with_prompt(prompt, timeout=900)
                        # Auto-commit if Claude left uncommitted changes
                        if _auto_commit_if_needed():
                            pass  # committed
                        # Catch up any unpushed commits (Claude's or ours)
                        _auto_push_if_ahead()
                        head_after = get_git_head()
                        code_changed = head_before != head_after and head_after != ""
                        _log_improvement("full_analysis", severe[0]["name"],
                                         success=True, code_changed=code_changed,
                                         output_preview=output[:500])
                        agent_proc = start_agent(args.agent_args)
                        last_analysis = time.time()
            else:
                # Level 1 only — just run analysis for logging
                run_full_analysis(args.minutes)

    except KeyboardInterrupt:
        _alert("Shutting down...")
        stop_agent(agent_proc)
        _alert("Done.")


if __name__ == "__main__":
    main()
