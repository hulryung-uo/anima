"""Offline analyzer for the P0 meta-controller shadow log.

The meta-controller (P0) writes one JSONL row per decision to
`data/meta_shadow.jsonl` recording *what mode it would have picked*
(`would_pick`) versus *what the planner actually ran* (`actual_mode`), tagged
with the `policy` that produced it (`llm` or `heuristic`). This module reads
that log and reports the would-pick-vs-actual agreement statistics that gate
the P0 -> P1 decision (see docs/meta-controller.md s8). It is a **pure offline
reader**: it imports nothing from the runtime, sends no packets, and drives no
behaviour -- it only summarizes an artifact the shadow stage already produced.

Run:

    python3 -m anima.planner.shadow_analysis [path/to/meta_shadow.jsonl]
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Default location must match meta_controller._SHADOW_LOG.
_DEFAULT_LOG = Path(__file__).parent.parent.parent / "data" / "meta_shadow.jsonl"


@dataclass
class PolicyStats:
    """Agreement summary for one policy population (e.g. "heuristic")."""

    policy: str
    rows: int = 0
    agreements: int = 0
    # how often the would-pick differed from the planner's actual mode, keyed
    # by "actual->would_pick" so a human can see *where* the controller diverges.
    divergences: Counter[str] = field(default_factory=Counter)
    # distribution of would_pick / actual_mode for a quick sanity check.
    would_pick_counts: Counter[str] = field(default_factory=Counter)
    actual_counts: Counter[str] = field(default_factory=Counter)

    @property
    def agreement_rate(self) -> float:
        """Fraction of rows where would_pick == actual_mode (0.0 if no rows)."""
        return self.agreements / self.rows if self.rows else 0.0

    @property
    def divergence_rate(self) -> float:
        return 1.0 - self.agreement_rate if self.rows else 0.0


@dataclass
class ShadowReport:
    total_rows: int = 0
    malformed_rows: int = 0
    by_policy: dict[str, PolicyStats] = field(default_factory=dict)

    @property
    def overall_agreement_rate(self) -> float:
        graded = sum(s.rows for s in self.by_policy.values())
        agreed = sum(s.agreements for s in self.by_policy.values())
        return agreed / graded if graded else 0.0

    def render(self) -> str:
        lines = [
            f"shadow rows: {self.total_rows}"
            f"  malformed: {self.malformed_rows}"
            f"  overall agreement: {self.overall_agreement_rate:.1%}",
        ]
        for policy in sorted(self.by_policy):
            s = self.by_policy[policy]
            lines.append(
                f"  [{policy}] rows={s.rows} agree={s.agreement_rate:.1%}"
                f" diverge={s.divergence_rate:.1%}"
            )
            for transition, n in s.divergences.most_common(5):
                lines.append(f"      {transition}: {n}")
        return "\n".join(lines)


def _grade_row(rec: dict, report: ShadowReport) -> None:
    """Fold one parsed record into the report. Tolerates missing fields:
    `agree` is recomputed from would_pick/actual_mode when absent or stale."""
    policy = str(rec.get("policy") or "unknown")
    would = rec.get("would_pick")
    actual = rec.get("actual_mode")
    if would is None or actual is None:
        report.malformed_rows += 1
        return

    stats = report.by_policy.setdefault(policy, PolicyStats(policy=policy))
    stats.rows += 1
    stats.would_pick_counts[would] += 1
    stats.actual_counts[actual] += 1
    # Recompute agreement from the modes themselves -- never trust a possibly
    # stale/missing `agree` flag written by an older log format.
    if would == actual:
        stats.agreements += 1
    else:
        stats.divergences[f"{actual}->{would}"] += 1


def analyze_shadow_log(path: str | Path | None = None) -> ShadowReport:
    """Read a meta-controller shadow JSONL and summarize agreement stats.

    Never raises on a missing file or malformed lines: a missing/empty log
    yields an all-zero report, and unparseable lines are counted in
    `malformed_rows` rather than aborting the analysis.
    """
    log_path = Path(path) if path is not None else _DEFAULT_LOG
    report = ShadowReport()
    if not log_path.exists():
        return report

    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            report.total_rows += 1
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                report.malformed_rows += 1
                continue
            if not isinstance(rec, dict):
                report.malformed_rows += 1
                continue
            _grade_row(rec, report)
    return report


def _main(argv: list[str]) -> int:
    path = argv[0] if argv else None
    report = analyze_shadow_log(path)
    if report.total_rows == 0:
        where = path or str(_DEFAULT_LOG)
        print(f"no shadow rows found at {where}")
        return 0
    print(report.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
