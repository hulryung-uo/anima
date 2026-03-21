#!/usr/bin/env python3
"""Supervisor — run agent + self-improvement in one process.

Cycle:
  1. Start agent (uv run python -m anima)
  2. Wait for analysis interval
  3. Analyze logs → detect problems
  4. If HIGH/CRITICAL → call Claude Code to fix
  5. If code changed → restart agent
  6. Repeat

Usage:
    uv run python tools/supervisor.py
    uv run python tools/supervisor.py --no-claude     # analyze only, no auto-fix
    uv run python tools/supervisor.py --interval 300  # analyze every 5 min
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
AGENT_CMD = [sys.executable, "-m", "anima"]

# How long to let the agent run before first analysis
WARMUP_SECONDS = 60


def get_git_head() -> str:
    """Get current git HEAD commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def start_agent(extra_args: list[str] | None = None) -> subprocess.Popen:
    """Start the agent as a subprocess."""
    cmd = AGENT_CMD + (extra_args or [])
    print(f"[supervisor] Starting agent: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        # Share stdout/stderr so we can see agent logs
        stdout=None,
        stderr=None,
    )
    print(f"[supervisor] Agent started (PID {proc.pid})")
    return proc


def stop_agent(proc: subprocess.Popen) -> None:
    """Gracefully stop the agent subprocess."""
    if proc.poll() is not None:
        print(f"[supervisor] Agent already exited (code {proc.returncode})")
        return

    print(f"[supervisor] Stopping agent (PID {proc.pid})...")
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
        print(f"[supervisor] Agent stopped (code {proc.returncode})")
    except subprocess.TimeoutExpired:
        print("[supervisor] Agent didn't stop, killing...")
        proc.kill()
        proc.wait()


def run_analysis(minutes: int) -> tuple[list[dict], str | None]:
    """Run log analysis. Returns (problems, report_path)."""
    from self_improve import detect_problems, generate_report, parse_recent_log, save_report

    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n[supervisor] [{ts}] Analyzing last {minutes} min...")

    data = parse_recent_log(minutes=minutes)
    if "error" in data:
        print(f"[supervisor] Analysis error: {data['error']}")
        return [], None

    problems = detect_problems(data)
    report = generate_report(data, problems)
    path = save_report(report)

    counts = data.get("counts", {})
    print(f"[supervisor] Log: {data.get('recent_lines', 0)} recent lines")
    print(
        f"[supervisor] Walk: {counts.get('walk_confirmed', 0)} OK / "
        f"{counts.get('walk_denied', 0)} denied"
    )
    print(f"[supervisor] Skills: {data.get('skill_success_rate', 0):.0%} success")
    print(f"[supervisor] Report: {path}")

    if problems:
        for p in problems:
            print(f"[supervisor]   [{p['severity']}] {p['name']}: {p['description']}")
    else:
        print("[supervisor] No problems detected.")

    return problems, str(path)


def run_claude_fix(report_path: str) -> bool:
    """Call Claude Code with the given report. Returns True if code changed."""
    from self_improve import call_claude

    print(f"[supervisor] Calling Claude Code with report: {report_path}")
    head_before = get_git_head()
    success = call_claude(Path(report_path))
    head_after = get_git_head()

    code_changed = head_before != head_after and head_after != ""
    if code_changed:
        print("[supervisor] Claude Code made changes — agent will restart")
    elif success:
        print("[supervisor] Claude Code ran but no changes committed")
    else:
        print("[supervisor] Claude Code failed")

    return code_changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Anima supervisor — agent + self-improvement")
    parser.add_argument(
        "--interval", type=int, default=600,
        help="Analysis interval in seconds (default: 600 = 10min)",
    )
    parser.add_argument(
        "--minutes", type=int, default=10,
        help="Minutes of log to analyze (default: 10)",
    )
    parser.add_argument(
        "--no-claude", action="store_true",
        help="Analyze only — don't call Claude Code for fixes",
    )
    parser.add_argument(
        "--agent-args", nargs="*", default=[],
        help="Extra args to pass to the agent (e.g. --tui --recreate)",
    )
    args = parser.parse_args()

    use_claude = not args.no_claude
    mode = "analyze + fix" if use_claude else "analyze only"
    print(f"[supervisor] Starting ({mode}, interval={args.interval}s)")
    print(f"[supervisor] Agent args: {args.agent_args}")
    print()

    # Add tools/ to path so we can import self_improve
    tools_dir = str(Path(__file__).parent)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    agent_proc = start_agent(args.agent_args)
    last_analysis = time.time()
    cycle = 0

    try:
        while True:
            # Check if agent is still running
            if agent_proc.poll() is not None:
                print(
                    f"[supervisor] Agent exited (code {agent_proc.returncode})"
                    f" — restarting in 10s..."
                )
                time.sleep(10)
                agent_proc = start_agent(args.agent_args)
                last_analysis = time.time()  # reset timer after restart
                continue

            now = time.time()
            # First analysis after warmup, then every interval
            wait_time = WARMUP_SECONDS if cycle == 0 else args.interval
            if now - last_analysis < wait_time:
                time.sleep(5)  # poll every 5s
                continue

            last_analysis = now
            cycle += 1

            # Run analysis
            problems, report_path = run_analysis(args.minutes)

            # If Claude mode enabled and severe problems found
            if use_claude and problems and report_path:
                severe = [p for p in problems if p["severity"] in ("HIGH", "CRITICAL")]
                if severe:
                    # Stop agent before Claude modifies code
                    stop_agent(agent_proc)

                    code_changed = run_claude_fix(report_path)

                    # Restart agent (with new code if changed)
                    if code_changed:
                        print("[supervisor] Restarting agent with updated code...")
                    else:
                        print("[supervisor] Restarting agent...")
                    agent_proc = start_agent(args.agent_args)
                    last_analysis = time.time()

    except KeyboardInterrupt:
        print("\n[supervisor] Shutting down...")
        stop_agent(agent_proc)
        print("[supervisor] Done.")


if __name__ == "__main__":
    main()
