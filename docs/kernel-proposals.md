# Kernel proposals (HUMAN-OWNED — agents must not edit `foundry/kernel/`)

The apprentice-growth analysis (critic-verified against the code) surfaced
several high-compounding improvements that live in the **trusted kernel**.
Per CLAUDE.md / FOUNDRY.md the kernel is human-owned — agents must not edit it.
This file collects them as concrete proposals for the human owner. Each
compounds across all 21 cells (it changes the *measurement*, not one genome),
which is why the analysis ranked "fix the signal first" above any single-cell
mutation.

Source: docs/growth-backlog.json (ranks 4, 11, 12). Line numbers verified
against the working tree at the time of writing — re-check before editing.

---

## P-1. Robust estimator for bimodal per-seed data (backlog rank 4)

**Where:** `foundry/kernel/eval.py` `_aggregate` (~517-543, uses
`statistics.fmean` field-by-field); `foundry/kernel/archive.py`
`reliability_score` (38-44) = `fmean - PROMOTION_LAMBDA*pstdev`.

**Problem:** many elites are **bimodal, not noisy** — two regimes (run worked
vs setup failed), and the mean lands in the empty valley between them:
- `g_00118` per_seed `[8.67, 8.65, 8.65, 135.79, 141.85, 144.22]` → mean 74.6 (no seed is near 74)
- `g_00107` `[8.5, 8.6, 8.6, 98.7, 90, 114]` (same shape)
- `g_00102` has a lone `5.0` death-outlier among ~100-200.

`fmean`/`mean−λ·pstdev` describe neither cluster and collapse on regime-switching
data, so promotion/selection are dominated by which regime a coin-flip landed in.

**Proposal:** replace the point estimate with a **median + quantile (e.g. 25th
percentile / IQR) lower bound**. Majority-regime genomes then promote on their
*typical* behavior. Bonus: a large low-cluster is a signal of intermittent
fixed-start/setup failure the CV-topup doesn't currently diagnose — worth logging.

**Impact:** every promotion across 21 cells reads this estimator → system-wide
signal quality. Compounds with the editable rank-2 change (select.py already
switched to `reliability`; making `reliability` itself robust closes the loop).

---

## P-2. Liveness must credit stationary-but-progressing skills (backlog rank 11a)

**Where:** `foundry/kernel/fitness.py` `_liveness` (~86-98).

**Problem:** `_liveness` scores movement/action-variety only. Stationary trades —
Meditation, Spirit Speak, and any "stand and channel" skill — run the full window
alive and gaining skill yet score ~0.15 liveness, which the viability gate then
multiplies into the floor. Genomes `g_00004`/`g_00008` are the evidence. Whole
archetypes (parts of MAGIC/BARD) are **structurally unscorable**.

**Proposal:** credit liveness when a *skill is actively rising* even without
movement (e.g. count resolved skill-checks / skill delta as a liveness source,
not just position/action diversity). Keep the anti-spam grouping.

**Impact:** unblocks stationary MAGIC/bard rows; removes a systematic bias toward
mobile professions.

---

## P-3. Tighten the behavior-bonus gaming vector (backlog rank 11b)

**Where:** `foundry/kernel/fitness.py` behavior_bonus (~155).

**Problem:** behavior_bonus makes NONE/COMBAT cells ~1.7x noisier (CV 0.294 vs
0.176 elsewhere). `g_00118` *failed its profession*, wandered, and still banked a
high, volatile NONE score off the bonus — i.e. the bonus rewards undirected
wandering. It's a reward-hack surface.

**Proposal:** lower the per-hour caps and/or require *sustained presence* (not a
burst) to earn the bonus; cap its share of total fitness.

**Impact:** makes the noisiest ~21% of the grid trustworthy; stops "wander and
collect the bonus" from out-competing real work.

---

## P-4. Add a balance descriptor + balance_term (backlog rank 11c)

**Where:** `foundry/kernel/descriptor.py` (grid axes) + `foundry/kernel/fitness.py`.

**Problem:** the grid descriptor is **(profession × sociability) only**. A pure
single-skill grinder (`g_00101` fit=234, move=0) dominates a balanced, living
adventurer (`g_00102` fit=111). Without a balance signal, a correct
meta-controller (docs/meta-controller.md) that *varies* modes would be **selected
against** — the evolution gradient points away from "living."

**Proposal:** add an autonomy/balance axis or term that rewards running several
modes in a window (and, per docs/apprentice-track.md §6, an `autonomy_term` that
rewards *low GM-intervention / unassisted survival*, measured in a GM-free scored
eval). This is the prerequisite for *evolving* (not hand-writing) mode-switching.

**Impact:** aligns the selection gradient with the "living resident" goal;
unblocks meta-controller P3.

---

## P-5. Anatomy-30 warrior birth template (backlog rank 12)

**Where:** `foundry/kernel/gm.py` (~97, warrior fixed-start skills; sum already 140).

**Problem:** both COMBAT elites (`g_00102`, `g_00115`) independently converged on
"add Anatomy 30 to the adventurer birth template — every swing rolls Anatomy
(BaseWeapon.cs), opening a 5th gain stream." `g_00102` estimates Anatomy alone is
+62% fitness, and skill_term is the dominant term (46.1 of 111). But birth skills
are kernel-owned and the sum is already 140, so something must drop.

**Proposal:** drop **Healing 35** (redundant with the interleaved auto-bandage in
combat_loop) for **Anatomy 30**. Net: opens a 5th per-swing CheckSkill stream.

**Editable complement (already shippable, not kernel):** lower
`BANDAGE_REAPPLY_S` (combat_loop.py:76, currently 8.5s) so the existing
Healing+Anatomy passive rolls fire more often — captures part of the gain now and
improves alive_fraction without touching the kernel. (Hold until the COMBAT
re-engage change, backlog rank 3, is measured, to avoid confounding.)

**Impact:** ~+30-60% COMBAT skill_term per the elites' own estimate, once the
kernel edit lands; the bandage tweak captures part immediately.

---

## Suggested order

1. **P-1 (robust estimator)** — highest compounding; makes every other result
   trustworthy and pairs with the shipped rank-2 reliability switch.
2. **P-2 / P-3** — unblock stationary archetypes and de-noise the bonus cells.
3. **P-5** — concrete, well-evidenced COMBAT win (small kernel edit).
4. **P-4** — larger; do once the meta-controller (docs/meta-controller.md) is
   past P0 shadow and there's data to justify the balance axis.
