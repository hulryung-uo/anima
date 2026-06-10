"""Foundry trusted kernel — the anchor of truth (human-owned).

The kernel defines what "better" means (fitness), what "kind" means
(descriptor), how a genome is evaluated (eval), and how the archive's integrity
and safety gates are enforced. It is reverted to a pinned git SHA before every
evaluation, so any mutation the AI makes here is discarded — the score is always
computed by the canonical kernel.

HARD RULE: nothing in foundry/kernel/ may import from `anima/`. The anima package
is the mutator-editable genome body; if the kernel parsed trajectories or
computed fitness using anima code, the mutator could edit that code to lie about
its own performance. The kernel therefore re-implements its own minimal,
independent packet parsing from raw bytes (see trajectory.py).
"""
