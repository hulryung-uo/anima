"""Safety + integrity gates (FOUNDRY.md §2, §9) — kernel-owned.

Two jobs:
  1. Kernel integrity — before every eval the orchestrator reverts foundry/kernel
     to a pinned git ref, so any mutation the AI made to the ruler is discarded
     and fitness is always computed by the canonical kernel.
  2. Run guards — global limits + a kill switch so an evolution run can't run
     away (token/agent budget is enforced by the workflow layer; these are the
     coarse process-level backstops).

Uses git via subprocess. Does NOT import anima/.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

KERNEL_PATH = "foundry/kernel"

# Run guards (coarse backstops; fine budgeting is the orchestrator's job).
MAX_CONCURRENT_EVALS = 4        # parallel ServUO accounts/worktrees
MAX_GENOMES_PER_RUN = 500       # hard cap on a single evolution run
DEFAULT_EVAL_WINDOW_S = 900     # 15 min eval window (FOUNDRY.md §5)
EVAL_SEEDS = 1                  # multi-seed averaging (Phase 0 starts at 1)

KILL_SWITCH_FILE = "STOP"       # touch foundry/STOP to halt a run


def _git(repo: str | Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def kernel_tree_sha(repo: str | Path, ref: str = "HEAD") -> str:
    """SHA of the foundry/kernel subtree at `ref` — changes iff the kernel changes."""
    r = _git(repo, "rev-parse", f"{ref}:{KERNEL_PATH}")
    return r.stdout.strip() if r.returncode == 0 else ""


def head_sha(repo: str | Path) -> str:
    r = _git(repo, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else ""


def revert_kernel(repo: str | Path, pinned_ref: str) -> tuple[bool, str]:
    """Restore foundry/kernel to its pinned version, discarding any mutation.

    Runs `git checkout <pinned_ref> -- foundry/kernel`. This is the core
    anti-gaming guarantee (FOUNDRY.md §2): the mutator literally cannot ship a
    modified ruler into an eval.
    """
    r = _git(repo, "checkout", pinned_ref, "--", KERNEL_PATH)
    if r.returncode != 0:
        return False, r.stderr.strip()
    return True, "kernel reverted"


def kernel_is_clean(repo: str | Path, pinned_ref: str) -> bool:
    """True iff foundry/kernel matches the pinned ref with no local edits."""
    # tree sha must match the pin
    if kernel_tree_sha(repo, "HEAD") != kernel_tree_sha(repo, pinned_ref):
        return False
    # and there must be no unstaged/uncommitted changes under the kernel path
    r = _git(repo, "status", "--porcelain", "--", KERNEL_PATH)
    return r.returncode == 0 and not r.stdout.strip()


def kill_switch_active(foundry_root: str | Path = "foundry") -> bool:
    return (Path(foundry_root) / KILL_SWITCH_FILE).exists()
