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

ROOT = Path(__file__).parent.parent
AGENT_CMD = [sys.executable, "-m", "anima"]
STATE_FILE = ROOT / "data" / "state.json"
DB_FILE = ROOT / "data" / "anima.db"
CLAUDE_LOG = ROOT / "data" / "claude_code.log"
IMPROVEMENTS_LOG = ROOT / "data" / "improvements.jsonl"
HINTS_FILE = ROOT / "data" / "supervisor_hints.json"

WARMUP_SECONDS = 90
STUCK_THRESHOLD = 300      # 5 minutes no activity → stuck
IDLE_THRESHOLD = 600       # 10 minutes no progress → idle
MAX_RESTARTS_PER_HOUR = 4  # prevent restart loop — too fast kills server sessions


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
    print(f"[supervisor] Starting agent: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, cwd=str(ROOT))
    print(f"[supervisor] Agent PID {proc.pid}")
    return proc


def stop_agent(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    print(f"[supervisor] Stopping agent PID {proc.pid}...")
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
    """Check action_logs for stuck loop patterns (last 5 minutes)."""
    if not DB_FILE.exists():
        return None
    try:
        conn = sqlite3.connect(str(DB_FILE))
        cutoff = time.time() - 300  # last 5 min

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
                        return "agent alive but zero procedures in 5min (planner idle loop)"
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
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[supervisor] [{ts}] AUTO-RECOVER: {reason}")

    stop_agent(proc)

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

    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[supervisor] [{ts}] Full analysis (last {minutes} min)...")

    data = parse_recent_log(minutes=minutes)
    if "error" in data:
        print(f"[supervisor] Analysis error: {data['error']}")
        return [], None

    problems = detect_problems(data)
    report = generate_report(data, problems)
    path = save_report(report)

    print(f"[supervisor] Report: {path}")
    if problems:
        for p in problems:
            print(f"[supervisor]   [{p['severity']}] {p['name']}: {p['description']}")
    else:
        print(f"[supervisor] No problems detected.")

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


def _auto_commit_if_needed() -> bool:
    """Commit uncommitted changes left by Claude Code. Returns True if committed."""
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
        if result.returncode == 0:
            print("[supervisor] Auto-committed uncommitted Claude Code changes")
            return True
        else:
            print(f"[supervisor] Auto-commit failed: {result.stderr[:200]}")
            return False
    except Exception as e:
        print(f"[supervisor] Auto-commit error: {e}")
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
    print(f"[supervisor] Starting ({mode}, interval={args.interval}s)")

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

            # Check if agent process is alive
            if agent_proc.poll() is not None:
                print(f"[supervisor] Agent exited (code {agent_proc.returncode})")
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
                        print(f"[supervisor] tools/ changed ({last_git_head[:8]} → {current_head[:8]}) — restarting supervisor")
                        stop_agent(agent_proc)
                        os.execv(sys.executable, [sys.executable] + sys.argv)

                    print(f"[supervisor] Code changed ({last_git_head[:8]} → {current_head[:8]}) — restarting agent")
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
                            print(f"[supervisor] Backoff: waiting {wait}s before recovery #{consecutive_recoveries + 1}")
                            time.sleep(wait)
                        restarts_this_hour.append(now)
                        consecutive_recoveries += 1
                        agent_proc = auto_recover(agent_proc, problem, args.agent_args)
                        last_analysis = now
                        continue
                    else:
                        print(f"[supervisor] ⚠ Too many restarts ({len(restarts_this_hour)}/hr), skipping")
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
                            ts = datetime.now().strftime("%H:%M:%S")
                            print(f"[supervisor] [{ts}] Skipping {fix_key} — already failed {attempts} fix attempts")
                            _log_improvement(
                                f"skip:{worst['procedure']}",
                                f"gave up after {attempts} failed fix attempts for {worst['top_failure']}",
                                success=False, code_changed=False,
                            )
                            _write_skip_hint(worst["procedure"], worst["top_failure"])
                        else:
                            proc_name = worst["procedure"]
                            timeout = _get_timeout(attempts)
                            ts = datetime.now().strftime("%H:%M:%S")
                            print(f"[supervisor] [{ts}] TARGETED FIX: {proc_name} ({worst['top_failure']}) attempt={attempts + 1} timeout={timeout}s")
                            stop_agent(agent_proc)
                            prompt = build_diagnostic_prompt(failure=worst)
                            head_before = get_git_head()
                            lock_key = f"targeted_fix:{fix_key}"
                            with FixLock(lock_key, sha=head_before) as got:
                                if not got:
                                    ts_str = datetime.now().strftime("%H:%M:%S")
                                    print(
                                        f"[supervisor] [{ts_str}] {lock_key} "
                                        f"already being fixed — skipping"
                                    )
                                    agent_proc = start_agent(args.agent_args)
                                    last_analysis = time.time()
                                    continue
                                success, output = call_claude_with_prompt(prompt, timeout=timeout)
                            # Auto-commit if Claude left uncommitted changes
                            if _auto_commit_if_needed():
                                pass  # committed
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
                                print(f"[supervisor] Targeted fix committed for {proc_name}")
                                fix_attempts[fix_key] = 0
                            elif success:
                                print(f"[supervisor] Claude ran but no changes for {proc_name}")
                                fix_attempts[fix_key] = attempts + 1
                            else:
                                print(f"[supervisor] Claude failed for {proc_name}")
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
                                ts_str = datetime.now().strftime("%H:%M:%S")
                                print(
                                    f"[supervisor] [{ts_str}] {lock_key} "
                                    f"already being fixed — skipping"
                                )
                                agent_proc = start_agent(args.agent_args)
                                last_analysis = time.time()
                                continue
                            _, output = call_claude_with_prompt(prompt, timeout=900)
                        # Auto-commit if Claude left uncommitted changes
                        if _auto_commit_if_needed():
                            pass  # committed
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
        print("\n[supervisor] Shutting down...")
        stop_agent(agent_proc)
        print("[supervisor] Done.")


if __name__ == "__main__":
    main()
