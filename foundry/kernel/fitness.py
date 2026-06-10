"""Fitness — the read-only oracle (FOUNDRY.md §5).

    fitness = viability_gate × (skill_term + worth_term + produce_term + behavior_bonus)

All terms are per-hour rates derived purely from a parsed trajectory (server
packets), never agent self-report. Weights live here and are kernel-owned: the
mutator may not edit them (editing the ruler == gaming the score). Fitness ranks
genomes WITHIN a behavior cell only (§4); cross-cell comparison is never needed.

Calibration note: the raw rates have different natural magnitudes (skill points
~0-30/hr vs gold ~hundreds/hr). To keep skill the backbone with the locked §5
weights (1.0 / 0.3 / 0.2), economy rates are normalized into "skill-point-
equivalent" units via GOLD_NORM before weighting. GOLD_NORM and the behavior
sub-weights are starting guesses to be calibrated in Phase 0 against fitness
variance — but they are facts of the ruler, not of the agent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from foundry.kernel import uoconst
from foundry.kernel.trajectory import TrajectorySummary

# --- locked weights (FOUNDRY.md §5) -----------------------------------------
W_SKILL = 1.0
W_WORTH = 0.3
W_PRODUCE = 0.2

# economy normalization: how many gold ≈ 1 skill point of value, so the skill
# backbone dominates typical ranges. (Phase-0 calibratable.)
GOLD_NORM = 20.0

# behavior_bonus sub-weights (per-hour rates already on a small scale).
WB_EXPLORE = 0.10   # unique regions / hr
WB_SOCIAL = 0.15    # speech that drew responses / hr
WB_COMBAT = 0.10    # damage dealt / hr (normalized)
DAMAGE_NORM = 10.0

# Caps on per-hour behavior rates. Over a short window a brief burst extrapolates
# to absurd per-hour values (a 2-min walk -> 800 regions/hr; 8 system messages ->
# 80 "responses"/hr) that would swamp the skill backbone — that is window noise,
# not genuine exploration/socializing. Cap so the bonus stays bounded and skill
# stays the backbone. (Phase-0 calibratable; the real fix is longer eval windows
# + fixed-start-at-work, FOUNDRY.md §5/§12.)
MOBILITY_RATE_CAP = 60.0   # regions/hr
SOCIAL_RATE_CAP = 20.0     # responses/hr

# viability gate tuning
TARGET_ACTION_RATE = 30.0   # actions/hr to reach full liveness (~1 / 2 min)
MIN_DURATION_H = 1.0 / 60.0  # 1 minute floor to avoid rate blow-ups


@dataclass
class FitnessBreakdown:
    """Transparent component view — the observer/mutator reads this."""

    total: float = 0.0

    # gate
    viability_gate: float = 1.0
    alive_fraction: float = 1.0
    liveness: float = 0.0
    loop_penalty: float = 0.0

    # terms (post-weight, post-normalization)
    skill_term: float = 0.0
    worth_term: float = 0.0
    produce_term: float = 0.0
    behavior_bonus: float = 0.0

    # raw rates (pre-weight) for interpretability
    skill_gain_rate: float = 0.0
    networth_rate: float = 0.0
    produce_value_rate: float = 0.0
    regions_rate: float = 0.0
    social_response_rate: float = 0.0
    damage_rate: float = 0.0

    duration_h: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


def _liveness(summ: TrajectorySummary, dur_h: float) -> float:
    """0→1 anti-freeze factor: did the agent take varied, real actions?

    A frozen agent (no confirmed actions) → ~0. Requires ≥2 distinct action
    groups for full credit so spamming one action can't fake liveness.
    """
    action_rate = summ.total_actions / dur_h if dur_h > 0 else 0.0
    base = min(1.0, action_rate / TARGET_ACTION_RATE)
    distinct = len([g for g, n in summ.action_counts.items() if n > 0])
    variety = min(1.0, distinct / 2.0)
    # variety never fully zeroes a busy agent; weight it as a 0.5..1.0 multiplier
    return base * (0.5 + 0.5 * variety)


def _loop_penalty(summ: TrajectorySummary) -> float:
    """0→1 penalty for pathological repetition, inferred from the wire.

    The classic failure (which crippled the old self-improve loop) is walking
    into a wall: many DenyWalk for few confirms. We use the deny ratio as a
    server-grounded proxy for being stuck in a loop.
    """
    total_steps = summ.steps_confirmed + summ.steps_denied
    if total_steps < 5:
        return 0.0
    return min(1.0, summ.steps_denied / total_steps)


def _produce_value(summ: TrajectorySummary) -> float:
    """Gold-equivalent value of items the agent put into its own containers."""
    total = 0
    for graphic, amount, _ts in summ.items_into_pack:
        total += uoconst.ITEM_VALUES.get(graphic, uoconst.ITEM_VALUE_DEFAULT) * amount
    return float(total)


def compute_fitness(summ: TrajectorySummary) -> FitnessBreakdown:
    """Compute the fitness scalar + breakdown from a parsed trajectory."""
    dur_h = max(summ.duration_h, MIN_DURATION_H)

    alive = summ.alive_fraction()
    liveness = _liveness(summ, dur_h)
    loop_pen = _loop_penalty(summ)
    gate = alive * liveness * (1.0 - loop_pen)

    # raw rates
    skill_rate = summ.skill_gain_total / dur_h
    networth_rate = summ.gold_delta / dur_h
    produce_rate = _produce_value(summ) / dur_h
    regions_rate = summ.unique_regions / dur_h
    # social response: received speech that plausibly answered ours. Without
    # full dialogue threading we proxy it as min(recv, k*sent)+ a fraction of
    # recv, so a talker who gets replies scores, a spammer ignored does not.
    social_resp_rate = (
        min(summ.speech_recv, 3 * summ.speech_sent) + 0.0
    ) / dur_h
    damage_rate = summ.damage_dealt / dur_h

    # weighted terms (economy normalized into skill-point-equivalents)
    skill_term = W_SKILL * skill_rate
    worth_term = W_WORTH * (networth_rate / GOLD_NORM)
    produce_term = W_PRODUCE * (produce_rate / GOLD_NORM)
    behavior_bonus = (
        WB_EXPLORE * min(regions_rate, MOBILITY_RATE_CAP)
        + WB_SOCIAL * min(social_resp_rate, SOCIAL_RATE_CAP)
        + WB_COMBAT * (damage_rate / DAMAGE_NORM)
    )

    inner = skill_term + worth_term + produce_term + behavior_bonus
    total = gate * inner

    return FitnessBreakdown(
        total=total,
        viability_gate=gate,
        alive_fraction=alive,
        liveness=liveness,
        loop_penalty=loop_pen,
        skill_term=skill_term,
        worth_term=worth_term,
        produce_term=produce_term,
        behavior_bonus=behavior_bonus,
        skill_gain_rate=skill_rate,
        networth_rate=networth_rate,
        produce_value_rate=produce_rate,
        regions_rate=regions_rate,
        social_response_rate=social_resp_rate,
        damage_rate=damage_rate,
        duration_h=dur_h,
    )
