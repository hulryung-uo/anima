"""Apprentice soak track (A1, SHADOW) — docs/apprentice-track.md.

Runs ONE long continuous session (no fitness scoring) and measures whether the
agent can LIVE unassisted over a long horizon — the thing the 600s scored eval
can't see (it spawns mobs on the anchor and the agent rarely dies in 10 min, so
the Rank 3/6 flee/recover/re-engage code barely fires).

This stage is SHADOW: it only LOGS where a GM tutor *would* have intervened
(e.g. the agent died and didn't self-recover within a grace period); it does NOT
actually intervene. The success metric is autonomy — deaths self-rescued and
shadow-interventions-per-hour trending toward 0 (see §5). Actual GM intervention
+ fading is stage A2 (kernel-owned, proposed, not built here).

Kernel is read-only: we IMPORT the eval runner + trajectory parser (the
orchestrator does the same) but never edit foundry/kernel/.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from foundry.kernel.eval import ANIMA_ROOT, EvalConfig, run_eval_multi
from foundry.kernel.trajectory import TrajectorySummary

APPRENTICE_DIR = ANIMA_ROOT / "data" / "apprentice"
INTERVENTION_LOG = APPRENTICE_DIR / "interventions.jsonl"

# Dead longer than this without self-revival → a GM tutor would step in (A2).
# Used only to LABEL shadow interventions; nothing acts on it in A1.
RECOVERY_GRACE_S = 180.0
# A gap this long between server vitals samples is a coarse "went unresponsive"
# (stall) signal — secondary to death, and noisier (depends on update cadence).
STALL_GAP_S = 120.0


@dataclass
class DeathEvent:
    died_ts: float
    revived_ts: float | None
    dead_s: float
    self_rescued: bool          # revived before window end AND within grace
    needed_intervention: bool   # dead > grace (shadow: a GM would step in)


@dataclass
class SoakReport:
    profession: str
    fixed_start: str
    ok: bool
    duration_s: float
    duration_h: float
    # productivity over the long run
    alive_fraction: float
    skill_gain_total: float
    gold_delta: float
    total_actions: int
    mobiles_seen: int           # threat/target presence (0 = empty workplace)
    damage_taken: int
    # autonomy (the apprentice metric, §5)
    deaths: int
    self_rescued: int
    shadow_interventions: int
    interventions_per_hour: float
    self_rescue_rate: float
    longest_alive_stretch_s: float
    longest_sample_gap_s: float
    skills_gained: dict[str, float] = field(default_factory=dict)
    death_events: list[dict] = field(default_factory=list)


def analyze_deaths(summary: TrajectorySummary,
                   grace_s: float = RECOVERY_GRACE_S) -> list[DeathEvent]:
    """Reconstruct death/recovery intervals from the hp_samples timeline.

    A death starts at the first sample with hits<=0 and ends at the next sample
    with hits>0 (a resurrection) or at window end (never recovered)."""
    events: list[DeathEvent] = []
    death_ts: float | None = None
    for ts, hits, _ in summary.hp_samples:
        if hits <= 0 and death_ts is None:
            death_ts = ts
        elif hits > 0 and death_ts is not None:
            dead_s = ts - death_ts
            events.append(DeathEvent(
                died_ts=death_ts, revived_ts=ts, dead_s=dead_s,
                self_rescued=dead_s <= grace_s,
                needed_intervention=dead_s > grace_s,
            ))
            death_ts = None
    if death_ts is not None:  # died and never revived before the window ended
        dead_s = max(0.0, summary.end_ts - death_ts)
        events.append(DeathEvent(
            died_ts=death_ts, revived_ts=None, dead_s=dead_s,
            self_rescued=False, needed_intervention=True,
        ))
    return events


def _longest_alive_stretch(summary: TrajectorySummary) -> float:
    """Longest continuous interval the agent stayed alive (hits>0)."""
    if not summary.hp_samples:
        return summary.duration_s  # no vitals seen → assume alive throughout
    longest = 0.0
    alive = True
    stretch_start = summary.start_ts
    for ts, hits, _ in summary.hp_samples:
        if hits <= 0 and alive:          # died → close the current alive stretch
            longest = max(longest, ts - stretch_start)
            alive = False
        elif hits > 0 and not alive:     # revived → a new alive stretch begins
            stretch_start = ts
            alive = True
    if alive:
        longest = max(longest, summary.end_ts - stretch_start)
    return max(0.0, longest)


def _longest_sample_gap(summary: TrajectorySummary) -> float:
    """Longest gap between consecutive vitals/position samples — a coarse
    'went quiet' (possible stall) indicator."""
    ts_list = sorted(t for t, *_ in (summary.hp_samples + summary.positions))
    if len(ts_list) < 2:
        return 0.0
    return max(b - a for a, b in zip(ts_list, ts_list[1:]))


def build_report(summary: TrajectorySummary, profession: str, fixed_start: str,
                 ok: bool = True, grace_s: float = RECOVERY_GRACE_S) -> SoakReport:
    deaths = analyze_deaths(summary, grace_s)
    n_deaths = len(deaths)
    n_rescued = sum(1 for d in deaths if d.self_rescued)
    n_intervene = sum(1 for d in deaths if d.needed_intervention)
    dur_h = summary.duration_h or (summary.duration_s / 3600.0)
    skills_gained = {
        str(sid): round(s.gain, 2)
        for sid, s in summary.skills.items() if s.gain > 0
    }
    return SoakReport(
        profession=profession,
        fixed_start=fixed_start,
        ok=ok,
        duration_s=round(summary.duration_s, 1),
        duration_h=round(dur_h, 3),
        alive_fraction=round(summary.alive_fraction(), 3),
        skill_gain_total=round(summary.skill_gain_total, 2),
        gold_delta=summary.gold_delta,
        total_actions=summary.total_actions,
        mobiles_seen=summary.entities_seen,
        damage_taken=summary.damage_taken,
        deaths=n_deaths,
        self_rescued=n_rescued,
        shadow_interventions=n_intervene,
        interventions_per_hour=round(n_intervene / dur_h, 2) if dur_h > 0 else 0.0,
        self_rescue_rate=round(n_rescued / n_deaths, 3) if n_deaths else 1.0,
        longest_alive_stretch_s=round(_longest_alive_stretch(summary), 1),
        longest_sample_gap_s=round(_longest_sample_gap(summary), 1),
        skills_gained=skills_gained,
        death_events=[asdict(d) for d in deaths],
    )


def _write_artifacts(report: SoakReport, user: str, stamp: float) -> Path:
    APPRENTICE_DIR.mkdir(parents=True, exist_ok=True)
    out = APPRENTICE_DIR / f"soak-{user}-{int(stamp)}.json"
    out.write_text(json.dumps({"ts": stamp, "user": user, **asdict(report)},
                              ensure_ascii=False, indent=1))
    # Shadow GM-intervention log: one record per death that would have needed a
    # tutor (the capability backlog — docs/apprentice-track.md §4.3).
    with INTERVENTION_LOG.open("a") as f:
        for d in report.death_events:
            if d["needed_intervention"]:
                f.write(json.dumps({
                    "ts": stamp, "shadow": True, "cause": "death_unrecovered",
                    "profession": report.profession, "user": user,
                    "died_ts": d["died_ts"], "dead_s": round(d["dead_s"], 1),
                    "self_rescue_failed": True,
                }, ensure_ascii=False) + "\n")
    return out


def run_soak(profession: str, fixed_start: str, duration_s: int,
             user: str | None = None, grace_s: float = RECOVERY_GRACE_S,
             proxy_port: int = 2620, web_port: int = 8160) -> SoakReport:
    """Run one long session via the kernel eval (fitness ignored) and report.

    Reuses run_eval_multi with seeds=1 and a long window; the soak cares about
    the trajectory + events, not the fitness scalar.
    """
    stamp = time.time()
    user = user or f"soak{int(stamp) % 1679616:x}"
    cfg = EvalConfig(
        account_user=user, persona=profession, fixed_start=fixed_start,
        window_s=duration_s, proxy_port=proxy_port, web_port=web_port,
        lane=0, seed=0, repo_root=ANIMA_ROOT,
    )
    print(f"[soak] {profession}/{fixed_start} for {duration_s}s (user={user}) — "
          f"SHADOW, no scoring, no GM intervention")
    res = run_eval_multi(cfg, seeds=1)
    if not res.ok or res.summary is None:
        print(f"[soak] FAILED: {getattr(res, 'error', 'no summary')}")
        # still emit a minimal report so the failure is recorded
        report = SoakReport(
            profession=profession, fixed_start=fixed_start, ok=False,
            duration_s=0, duration_h=0, alive_fraction=0, skill_gain_total=0,
            gold_delta=0, total_actions=0, mobiles_seen=0, damage_taken=0,
            deaths=0, self_rescued=0, shadow_interventions=0,
            interventions_per_hour=0, self_rescue_rate=1.0,
            longest_alive_stretch_s=0, longest_sample_gap_s=0,
        )
    else:
        report = build_report(res.summary, profession, fixed_start,
                              ok=res.ok, grace_s=grace_s)
    out = _write_artifacts(report, user, stamp)
    _print_report(report, out)
    return report


def _print_report(r: SoakReport, out: Path) -> None:
    print(f"""
=== Apprentice soak report ({r.profession}/{r.fixed_start}) ===
  duration        {r.duration_s:.0f}s ({r.duration_h:.2f}h)  ok={r.ok}
  alive_fraction  {r.alive_fraction:.3f}   longest_alive_stretch {r.longest_alive_stretch_s:.0f}s
  productivity    skill +{r.skill_gain_total:.1f}, gold {r.gold_delta:+d}, actions {r.total_actions}
  environment     mobiles_seen {r.mobiles_seen}, damage_taken {r.damage_taken}
  AUTONOMY
    deaths                {r.deaths}
    self-rescued          {r.self_rescued}  (rate {r.self_rescue_rate:.2f})
    shadow interventions  {r.shadow_interventions}  ({r.interventions_per_hour:.2f}/h)  ← drive toward 0
  longest_sample_gap {r.longest_sample_gap_s:.0f}s{' (⚠ possible stall)' if r.longest_sample_gap_s > STALL_GAP_S else ''}
  report → {out}
  skills gained: {r.skills_gained or 'none'}
""")


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Apprentice soak track (A1, shadow)")
    ap.add_argument("--profession", default="adventurer")
    ap.add_argument("--fixed-start", default="warrior")
    ap.add_argument("--duration", type=int, default=1800, help="session seconds")
    ap.add_argument("--grace", type=float, default=RECOVERY_GRACE_S,
                    help="dead-longer-than → shadow GM intervention")
    ap.add_argument("--user", default=None)
    args = ap.parse_args(argv)
    run_soak(args.profession, args.fixed_start, args.duration,
             user=args.user, grace_s=args.grace)
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
