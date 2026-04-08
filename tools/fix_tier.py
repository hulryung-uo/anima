"""Micro-fix tier for the supervisor self-improvement loop.

Provides a fast Haiku-backed tier that tries obvious 1-5 line fixes
in ~30 seconds before escalating to Opus for full analysis.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent

TIER_QUICK = "quick"   # Haiku, 60s, simple patches
TIER_DEEP = "deep"     # Opus, 900s, full analysis

# Phrases that indicate Haiku is punting to a deeper tier
_ESCALATE_PHRASES = [
    "escalate",
    "too complex",
    "requires deeper analysis",
    "deeper analysis",
    "architectural",
    "multiple files",
]


def build_quick_prompt(diagnostic: str, problem: str) -> str:
    """Short, pattern-matched prompt for Haiku."""
    return f"""You are performing a MICRO-FIX for the Anima UO AI agent.
Your job: apply a small, obvious patch (1-5 lines) or say "ESCALATE".

Problem: {problem}

Diagnostic:
{diagnostic}

Rules:
- If you can fix it in 5 lines or fewer, apply the patch and commit.
- If the root cause is unclear OR requires understanding multiple files,
  respond with exactly "ESCALATE: <one-line reason>" and do NOT edit anything.
- Run `uv run pytest tests/ -q` before committing. Skip the commit if tests fail.
- Use `git commit -m "Micro-fix: <one-line summary>"`.
- Do not refactor. Do not add features. Do not touch unrelated files.

Your entire session should finish in under 60 seconds.
"""


def is_too_complex(output: str) -> bool:
    """Check if Haiku's output indicates the problem is beyond quick-fix.

    Returns True when Haiku's response contains phrases like "too complex",
    "requires deeper analysis", "escalate", or when no code was changed
    AND the output discusses architectural concerns.
    """
    if not output or not output.strip():
        return True

    lower = output.lower()
    return any(phrase in lower for phrase in _ESCALATE_PHRASES)


def _current_sha() -> str:
    """Return current git HEAD SHA, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def call_quick_fix(
    diagnostic: str,
    problem: str,
    timeout: int = 60,
    model: str = "claude-haiku-4-5-20251001",
) -> tuple[bool, bool, str]:
    """Call Haiku for a quick fix.

    Returns (success, committed, output).
    - success: subprocess exited cleanly
    - committed: git HEAD changed during the call
    - output: Claude stdout/stderr
    """
    sha_before = _current_sha()
    prompt = build_quick_prompt(diagnostic, problem)

    try:
        result = subprocess.run(
            ["claude", "--model", model, "-p", prompt],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        output = result.stdout or ""
        if result.stderr:
            output += "\n--- stderr ---\n" + result.stderr

        sha_after = _current_sha()
        committed = bool(sha_before and sha_after and sha_before != sha_after)
        success = result.returncode == 0
        return success, committed, output

    except subprocess.TimeoutExpired as e:
        partial = ""
        if e.stdout:
            partial = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else str(e.stdout)
        if e.stderr:
            stderr_text = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else str(e.stderr)
            partial = partial + "\n--- stderr ---\n" + stderr_text
        return False, False, f"Quick fix timed out after {timeout}s\n{partial[:500]}"

    except FileNotFoundError:
        return False, False, "Claude Code CLI not found"
