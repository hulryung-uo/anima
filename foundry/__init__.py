"""Anima Foundry — a self-developing agent system.

Foundry evolves a population of UO-playing agents (the `anima/` package is the
genome body) by mutating their code, evaluating each variant against a live
ServUO shard, and archiving the best of every behavioral kind (MAP-Elites).

Layout:
    foundry/kernel/   — HUMAN-OWNED. The ruler. Reverted to a pinned SHA before
                        every eval so the mutator cannot game its own score.
                        MUST NOT import anima/ (which the mutator can edit).
    foundry/          — mutator-editable improver (observe / select / mutate /
                        orchestrate). The AI may rewrite these.

Design: docs/FOUNDRY.md.
"""

__version__ = "0.0.1"
