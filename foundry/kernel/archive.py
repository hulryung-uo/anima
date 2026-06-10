"""Genome archive + MAP-Elites grid (FOUNDRY.md §3, §4, §7) — kernel-owned.

Stores every evaluated genome (append-only JSON) and maintains the QD grid: a
map from behavior cell -> the single highest-fitness genome with that behavior.
The promotion rule (who enters/replaces a cell) is integrity-critical and lives
here in the kernel; the *selection policy* (which parent to mutate next) is
mutator-editable and lives in foundry/select.py.

A genome:
    id          g_NNNNN (sequential, deterministic)
    parent      parent genome id (lineage), or None for a seed
    code_ref    git ref/SHA capturing the anima/ (+foundry/) code of this variant
    config      persona/params dict
    eval        {fitness, cell, descriptor, seed, trajectory_ref, breakdown}
    hypothesis  why this mutation was made (free text)
    ts          unix seconds (stamped by caller; kernel does not call the clock)

No imports from anima/.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


def cell_to_str(cell: tuple) -> str:
    return "|".join(str(p) for p in cell)


@dataclass
class Genome:
    id: str
    parent: str | None = None
    code_ref: str = ""
    config: dict = field(default_factory=dict)
    eval: dict = field(default_factory=dict)
    hypothesis: str = ""
    ts: float = 0.0

    @property
    def fitness(self) -> float:
        return float(self.eval.get("fitness", 0.0))

    @property
    def cell(self) -> tuple:
        return tuple(self.eval.get("cell", ()))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Genome":
        return cls(
            id=d["id"], parent=d.get("parent"), code_ref=d.get("code_ref", ""),
            config=d.get("config", {}), eval=d.get("eval", {}),
            hypothesis=d.get("hypothesis", ""), ts=d.get("ts", 0.0),
        )


@dataclass
class InsertResult:
    status: str          # "filled" (new cell) | "improved" | "rejected"
    cell: tuple
    fitness: float
    prev_fitness: float | None = None

    @property
    def entered_grid(self) -> bool:
        return self.status in ("filled", "improved")


class Archive:
    """File-backed genome store + MAP-Elites grid."""

    def __init__(self, root: str | Path = "foundry/archive") -> None:
        self.root = Path(root)
        self.genomes_dir = self.root / "genomes"
        self.grid_path = self.root / "grid.json"
        self.genomes_dir.mkdir(parents=True, exist_ok=True)
        # grid: cell_str -> genome_id (the current elite of that cell)
        self.grid: dict[str, str] = {}
        if self.grid_path.exists():
            try:
                self.grid = json.loads(self.grid_path.read_text())
            except (json.JSONDecodeError, OSError):
                self.grid = {}

    # ---- ids -------------------------------------------------------------
    def next_id(self) -> str:
        n = len(list(self.genomes_dir.glob("g_*.json"))) + 1
        return f"g_{n:05d}"

    # ---- persistence -----------------------------------------------------
    def _genome_path(self, gid: str) -> Path:
        return self.genomes_dir / f"{gid}.json"

    def save_genome(self, g: Genome) -> None:
        self._genome_path(g.id).write_text(json.dumps(g.to_dict(), indent=2))

    def get(self, gid: str) -> Genome | None:
        p = self._genome_path(gid)
        if not p.exists():
            return None
        return Genome.from_dict(json.loads(p.read_text()))

    def all_genomes(self) -> list[Genome]:
        out = []
        for p in sorted(self.genomes_dir.glob("g_*.json")):
            try:
                out.append(Genome.from_dict(json.loads(p.read_text())))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def _save_grid(self) -> None:
        self.grid_path.write_text(json.dumps(self.grid, indent=2))

    # ---- the promotion rule (integrity-critical) -------------------------
    def add(self, g: Genome) -> InsertResult:
        """Persist genome and insert into the grid iff it is the new elite.

        Rule: enter the cell if it is empty (filled) or this genome's fitness
        strictly exceeds the current occupant's (improved). Otherwise rejected.
        The genome is always persisted regardless of grid outcome (full lineage
        is kept; the grid only tracks elites).
        """
        self.save_genome(g)
        cell_str = cell_to_str(g.cell)
        cur_id = self.grid.get(cell_str)

        if cur_id is None:
            self.grid[cell_str] = g.id
            self._save_grid()
            return InsertResult("filled", g.cell, g.fitness, None)

        cur = self.get(cur_id)
        prev_fit = cur.fitness if cur else None
        if prev_fit is None or g.fitness > prev_fit:
            self.grid[cell_str] = g.id
            self._save_grid()
            return InsertResult("improved", g.cell, g.fitness, prev_fit)

        return InsertResult("rejected", g.cell, g.fitness, prev_fit)

    # ---- queries (read-only; selection policy lives in foundry/select.py)-
    def get_elite(self, cell: tuple) -> Genome | None:
        gid = self.grid.get(cell_to_str(cell))
        return self.get(gid) if gid else None

    def elites(self) -> list[Genome]:
        out = []
        for gid in self.grid.values():
            g = self.get(gid)
            if g:
                out.append(g)
        return out

    def filled_cells(self) -> int:
        return len(self.grid)

    def qd_score(self) -> float:
        """Sum of elite fitnesses — the standard QD progress metric (telemetry)."""
        return sum(g.fitness for g in self.elites())

    def best(self) -> Genome | None:
        es = self.elites()
        return max(es, key=lambda g: g.fitness) if es else None

    def summary(self) -> dict:
        es = self.elites()
        return {
            "total_genomes": len(list(self.genomes_dir.glob("g_*.json"))),
            "filled_cells": self.filled_cells(),
            "qd_score": round(self.qd_score(), 3),
            "best_fitness": round(self.best().fitness, 3) if es else 0.0,
            "cells": {c: gid for c, gid in sorted(self.grid.items())},
        }


def _main(argv: list[str]) -> int:
    root = argv[0] if argv else "foundry/archive"
    arc = Archive(root)
    print(json.dumps(arc.summary(), indent=2))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
