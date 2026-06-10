# Anima Foundry — Self-Developing Agent System (v3)

> *Anima는 AI 플레이어였다. Foundry는 그 AI 플레이어를 **스스로 개발하는** AI다.*

This document supersedes the self-improvement loop described in `DESIGN.md §3.3` and
`README.md` (the "self-improvement loop"). The old loop had its intelligence in the
wrong layer: a hardcoded rule engine (`detect_problems`) diagnosed problems and Claude
merely patched a pre-diagnosed bug. Foundry moves the intelligence **up** — the AI owns
the entire develop cycle: observe → hypothesize → design → implement → evaluate →
select → remember.

## 1. The Core Reframe

| Old (`tools/self_improve.py` + `supervisor.py`) | New (Foundry) |
|---|---|
| One mutable git HEAD, overwritten each fix | **Archive of evaluated genomes** + lineage |
| Hardcoded `detect_problems` rules decide what's wrong | **LLM forms hypotheses** from observations |
| Optimize toward "no rule violations" | **Quality-Diversity**: fill a grid of diverse, capable agents |
| Claude = hired hand fixing a known bug | **Claude = mutation operator** in an evolutionary loop |
| Game = thing being debugged | **Game = fitness environment** (read-only oracle) |
| Single account, serial, slow | **Local ServUO + N accounts, parallel** eval fleet |

One line:

> **Game world = fitness environment. Claude = mutation operator. Agents = evolving
> population, selected for behavioral diversity, archived by lineage.**

This fuses the project's two goals into one mechanism: *"AI develops AI agents"* and
*"a living world of diverse souls"* become the same loop — MAP-Elites filling a grid of
distinct, capable inhabitants.

## 2. The Trusted Kernel (human-owned, AI cannot edit)

The kernel is the **anchor of truth**. Everything else in the repo is editable by the
mutator — including the improver itself. The kernel is the small surface that is not,
because it defines "better" and guards integrity. This is the pragmatic Gödel-machine
compromise: the AI can improve the AI that improves the AI, but it cannot rewrite the
ruler that measures success.

```
foundry/kernel/          ← HUMAN-OWNED. Reverted to a pinned SHA before EVERY eval.
  fitness.py             ← the read-only oracle: defines the fitness scalar (§5).
  descriptor.py          ← computes the behavior descriptor (QD axes) from a trajectory.
  eval.py                ← runs a genome on a ServUO account, returns (fitness, descriptor, trajectory).
  archive.py             ← genome store integrity + promotion rule (who enters the grid).
  safety.py              ← worktree isolation, push gate, resource/kill limits, token budget.
```

**Enforcement of "AI cannot edit the kernel":**
- Before each eval the orchestrator runs `git checkout <KERNEL_SHA> -- foundry/kernel/`,
  so any mutation to the kernel is reverted and fitness is **always computed by the
  canonical kernel**.
- Fitness is computed from **server trajectory packets** (ground truth via `uo_proxy`),
  not from the agent's own logs. The agent cannot lie about "I made 100 gold" — the
  server packets say what actually happened.
- The mutator **never reports its own score**. The kernel computes it.

Changing the kernel is a deliberate human act (a new pinned SHA), never an autonomous one.

## 3. The Genome

The unit that gets evaluated and archived.

```jsonc
// foundry/archive/<genome_id>.json
{
  "id": "g_00417",
  "parent": "g_00391",          // lineage
  "code_ref": "a1b2c3d",        // git tree SHA of anima/ (+ foundry/ if self-improved)
  "config": { "persona": "...", "params": {...} },
  "eval": {
    "fitness": 1342.5,          // scalar, computed by kernel/fitness.py (§5)
    "descriptor": [2, 0, 1, 3], // QD cell coordinates, from kernel/descriptor.py
    "seed": 90871,
    "trajectory_ref": "data/trajectories/g_00417.jsonl"
  },
  "hypothesis": "miner died to PKs at HP<50%; added early flee",  // why this mutation
  "ts": 1781020800
}
```

Code lives in git (cheap, diffable); the archive indexes it. A genome is reproducible:
same `code_ref` + same `seed` → same eval (modulo server nondeterminism, mitigated by
fixed spawn state and short eval windows).

## 4. The QD Grid — Behavior Descriptor (LOCKED — kernel-owned)

Decided 2026-06-09. The descriptor is computed by `foundry/kernel/descriptor.py` *from the
trajectory* (server packets via `uo_proxy`, never the code or agent logs — **behavior, not
intent**). It captures **what kind** of soul the agent is; fitness (§5) captures **how good**
at being that kind. The two are deliberately decoupled: the descriptor reads *style/identity*,
never *competence*.

### The 4 locked axes

| Axis | Signal (server packets) | Captures | Type |
|---|---|---|---|
| `profession_focus` | skill-gain (0x3A) grouped by category, argmax | livelihood identity | categorical |
| `sociability` | speech+forum actions / total actions | hermit ↔ socialite | continuous |
| `aggression` | combat-initiation actions / total actions | pacifist ↔ killer | continuous |
| `mobility` | unique tiles/regions visited per hour | homebody ↔ wanderer | continuous |

**`profession_focus` bins (categorical):**
`GATHERING` · `CRAFTING` · `COMBAT` · `MAGIC` · `BARD-SOCIAL` · `THIEF-STEALTH` · `NONE`.
The `NONE` bin is where agents that grind no significant skill land — pure explorers and
socialites — and they are ranked there by fitness's Tier-3 `behavior_bonus` (§5). This is
exactly how the descriptor and fitness interlock.

**Continuous axes:** discretized into **3 bins** (low / mid / high) to start; boundaries
calibrated in Phase 0. The signals are *fractions/rates of action types* (not success), so
they stay orthogonal to fitness.

### Fitness-orthogonality (the subtle property)
`profession_focus` reads *which* skill (category), not *how much* (that is the fitness
backbone). The temperament axes read action-type fractions, not outcomes. So the descriptor
is largely fitness-orthogonal — the grid expresses genuine diversity, not a fitness ramp.
Partial correlation remains (a `COMBAT` agent tends to high `aggression`); that is
**information, not breakage** — an empty "pacifist swordsman" cell is a meaningful gap, and
MAP-Elites tolerates uneven cell density.

### Curse-of-dimensionality management (phased activation)
Full 4-axis grid = 7 × 3 × 3 × 3 = **189 cells**. Like fitness, the full design is locked but
activation is phased so the grid actually fills:
- **Phase 0–2 start: `profession_focus × sociability`** = 7 × 3 = **21 cells**. The most
  fitness-orthogonal pair — produces real within-profession diversity ("social miner" vs
  "hermit miner"). Matches the "a living world needs talkers *and* loners" vision.
- **Phase 2+**: activate `aggression`, then `mobility`, as the grid fills.

### Stability under noise
Per-eval descriptors are averaged across the multi-seed runs (§5): categorical
`profession_focus` by mode, continuous axes by mean-then-bin.

### How the grid works
Each unique bin-combination is a **cell** holding the single highest-fitness genome with
that behavior — the museum of diverse souls the mutator tries to *fill and improve*.
- **Improvement** = a new genome beats its cell's current occupant → replaces it.
- **Exploration** = a new genome lands in an *empty* cell → fills it (diversity gained, even
  at low fitness).

**Fitness ranks within a cell only.** A bard and a miner never compete — they live in
different cells. Parent selection uses cell *occupancy* (frontier bias), not cross-cell
fitness. Global progress is the QD-score (sum of cell elites), telemetry only. So the
algorithm **never needs cross-behavior fitness comparability** — this is what keeps the
design tractable.

### Documented future axes (Phase 4+)
`risk_appetite` (dungeon-depth / HP-danger exposure: cautious ↔ reckless) and
`self_sufficiency` (gather-own ↔ buy/trade: autarky ↔ merchant). Added only when the
4-axis grid is filling well — each new axis multiplies cell count.

## 5. Fitness Specification (LOCKED — kernel-owned)

Decided 2026-06-09. Weights live in `foundry/kernel/fitness.py` and are **not** editable
by the mutator — editing the ruler is gaming the score. Fitness is a scalar, computed per
genome from its eval trajectory (server packets via `uo_proxy`, never agent self-report).
It ranks genomes **within a cell only** (§4).

```
fitness = viability_gate × ( skill_term + worth_term + produce_term + behavior_bonus )

viability_gate = alive_fraction × liveness × (1 − loop_penalty)     # ∈ [0,1], HARD gate

skill_term     = 1.0 × skill_gain_rate       # BACKBONE: server-confirmed skill points / hr
worth_term     = 0.3 × networth_delta_rate   # (gold + bankable item value) Δ / hr
produce_term   = 0.2 × items_value_rate      # net retained crafted/gathered value / hr
behavior_bonus = descriptor-aligned reward / hr   # Tier 3 (§4), carries expressive archetypes
```

All accumulation terms are **rates (per hour)** so eval-window jitter cancels.

### Tier 1 — viability_gate (hard multiplicative floor)
A broken agent scores ~0 regardless of everything else. This replaces the old
`detect_problems` rule engine. Multiplicative by design: you cannot score by merely
"being alive" — the gate only *prevents* scoring when broken.

| Factor | Definition | Server source |
|---|---|---|
| `alive_fraction` | (ticks alive) / (eval ticks). Graded, not binary — 18/20 min then death = 0.9 | HP / death packets |
| `liveness` | ramps 0→1 as distinct *confirmed* action types pass a floor (anti-freeze) | ConfirmWalk 0x22, gump, skill packets |
| `loop_penalty` | fraction of actions that are pathological repeats of a failing procedure | action_logs + server responses |

### Tier 2 — skill backbone (universal competence)
`skill_gain_rate` = weighted sum of server-confirmed skill-point deltas per hour. Chosen as
the backbone because skill gain is **server-authoritative** (0x3A packets, unfakeable),
**near-universal** across professions (miner→Mining, bard→Musicianship, mage→Magery), and
**self-throttling** — UO drops gain chance as skill rises and caps it, so gaming resistance
is built into the game mechanic, not bolted on. `worth_term` and `produce_term` are
secondary economic signals at low weight.

### Tier 3 — behavior_bonus (QD spirit)
Selected by the genome's descriptor cell so each cell's elite is the best *of its kind*:
explorer → unique regions/hr; social → conversations that drew a response/hr; aggressive →
kills/hr. Carries expressive archetypes (a pure socialite gains ~0 skill but scores here).
Tier 2 and Tier 3 are **additive** — weakness in one is survivable via the other — but both
are gated by Tier 1.

### What fitness deliberately does NOT reward
- **No milestone** for completing mine→smelt→craft→sell→bank. Completing the loop *emerges*
  as high skill+worth+produce. Reward outcomes, not prescribed procedures — this is what
  keeps the search open-ended and lets a *better* lifestyle outscore the human-assumed one.
- **Nothing** computed from the agent's own logs. Server ground truth only.

### Eval protocol (noise control)
- **Fixed start state**: standard spawn + standard starting inventory (no consumables to
  front-load), seeded.
- **Window**: 10–20 min wall-clock; every term rate-normalized so jitter cancels.
- **Trust threshold**: re-run the same genome ≥5× (multi-seed). If σ is large relative to
  inter-genome spread, lengthen the window or add seeds. Calibrated in Phase 0.

Anti-gaming guarantees for each term are enumerated in §9.

## 6. The Develop Cycle (the meta-agent = "the Developer")

One cycle, run by an LLM agent (Claude Code) inside an isolated git worktree:

```
1. SELECT parent   — pick a genome from the archive.
                     QD selection: bias toward frontier cells (empty-adjacent),
                     recently-improved cells, and high-fitness elites. Uniform-random
                     over filled cells is the baseline.
2. OBSERVE         — read the parent's eval trajectory + foundry/observe.py diagnostics
                     (the old diagnose.py, but the INTERPRETATION is now the LLM's).
                     Also read an archive summary: which cells are filled / empty.
3. HYPOTHESIZE     — either a FIX ("flee earlier to survive") or an EXPLORE
                     ("no agent fills the 'social bard' cell — make one talk more").
4. MUTATE          — edit anima/ (and optionally foundry/, but never foundry/kernel/).
5. EVAL            — kernel/eval.py runs the variant on a ServUO account →
                     (fitness, descriptor, trajectory).
6. ARCHIVE         — kernel/archive.py inserts iff new-best-for-cell or fills-empty-cell.
                     Lineage recorded. Promotion to main only after held-out re-eval.
7. REFLECT         — append to the evolution log; optionally update a learned prior of
                     "which mutation kinds tend to work" that the mutator reads next cycle.
```

Cost tiering (kept from `fix_tier.py`): try a cheap model for obvious mutations, escalate
to a stronger model when the hypothesis is architectural.

## 7. The Orchestrator (replaces `supervisor.py`)

- Maintains a pool of **K worktrees × K ServUO accounts** on a local shard.
- Runs N develop cycles **in parallel** — each cycle is independent (mutate+eval on its
  own account/worktree) → embarrassingly parallel. This is where local-ServUO-parallel
  eval pays off.
- Owns safety: rate limits, token budget, max concurrent agents, kill switch, the
  kernel-revert-before-eval step, and the main-branch push gate.
- Replaces `fix_lock.py` (worktree isolation makes file locks unnecessary) and folds in
  the cost-tiering of `fix_tier.py`.

## 8. Two Learning Timescales (clean separation)

- **Intra-life (fast)**: the existing RL Q-table / UCB1 lets an *individual* adapt during
  its eval run (which skill to pick now). Unchanged.
- **Cross-life (slow)**: evolution operates *across* lives — the mutator changes the code
  that the next generation is born with.

This is a clean Baldwin-effect split: individuals learn within a life; the population
evolves the priors they're born with. The two never fight because they act on different
objects (Q-values vs. source code).

## 9. Anti-Reward-Hacking

The single most important failure mode of any self-improving system. Defenses:

1. **Kernel revert before eval** — the mutator literally cannot ship a modified ruler.
2. **Fitness from server packets, not agent logs** — ground truth, not self-report.
3. **Skill backbone is self-throttling** — UO's gain-chance decay + skill caps bound the
   dominant term; no infinite farming.
4. **Net (retained) value, not gross events** — craft→destroy→craft can't inflate
   `produce_term`; crafting consumes server-enforced resources.
5. **Viability is a gate, not a reward** — faking liveness gets you off zero but earns
   nothing on its own; you still need Tier 2/3.
6. **Held-out re-eval** — periodically re-run archive champions on fresh seeds to catch
   overfitting and lucky runs; demote genomes that don't replicate.
7. **Behavior descriptor from trajectory** — the agent can't fake which cell it's in.
8. **Push gate** — a variant reaches `main` only after beating the incumbent on held-out
   eval. Humans retain veto.

## 10. How Existing Assets Are Repurposed (rebuild ≠ discard)

| Today | Foundry role |
|---|---|
| `tools/diagnose.py` | → `foundry/observe.py` — diagnostics the mutator *reads* (LLM interprets) |
| `tools/self_improve.py` `detect_problems` | retired as a gate; optional cheap "smell" hint only |
| `tools/supervisor.py` (3-level loop) | → `foundry/orchestrator.py` (parallel cycle scheduler + safety) |
| `tools/fix_lock.py` | retired — worktree isolation replaces file locks |
| `tools/fix_tier.py` | folded into mutator cost-tiering |
| `anima/` (whole agent) | the **evolvable genome body** — mostly unchanged, it's what evolves |
| `uo_proxy/` trajectories | fitness + behavior-descriptor extraction; offline analysis |
| RL Q-table (`anima/memory/`) | intra-life adaptation (§8) |
| ServUO multi-account (Phase 7) | repurposed as the **eval fleet** |
| Persona YAML | seeds + coordinates in the QD grid |

## 11. Phased Rollout (buildable, not a moonshot)

- **Phase 0 — Kernel + single-cell loop.** ✅ **BUILT (2026-06-10).** `kernel/eval.py`
  (live eval through uo_proxy), `kernel/fitness.py` (§5 skill-backbone),
  MAP-Elites archive, serial develop loop. Fixed-start implemented as designed:
  `kernel/gm.py` (a kernel-owned minimal wire client; GameMaster account provisioned
  by `kernel/provision.py`) teleports the eval char to its workplace, pins the
  profession skill, hands tools — all by remote serial targeting; the scored window
  starts after setup and `trajectory.parse_file(window_start=…)` keeps setup out of
  fitness (skill sets = baseline shifts; GM-given items never count as production).
- **Phase 1 — Parallelism.** ✅ **BUILT (2026-06-10).** K slot worktrees
  (`.worktrees/slotN`) × K auto-created accounts; orchestrator runs develop cycles
  concurrently (mutation + eval in the worktree, agent spawned from it), archive
  inserts single-writer, variant commits pinned under `refs/foundry/<genome>`;
  multi-seed eval averaging (`run_eval_multi`). The scoring kernel always runs from
  the MAIN repo; uo_proxy too.
- **Phase 2 — MAP-Elites grid.** Add `kernel/descriptor.py`, multi-cell archive,
  frontier-biased selection. QD kicks in — diverse agents emerge.
- **Phase 3 — Self-improving improver.** Open `foundry/mutate.py` + `select.py` to the
  mutator. It tunes its own hypothesis/selection strategy. **Kernel stays locked.**
- **Phase 4 — Open-ended.** Mutator may propose new behavior axes, new skills, new
  personas. The grid grows dimensions; the living world fills itself.

## 12. Open Questions (to resolve as we build)

- **Fitness weights & window** — `0.3 / 0.2` and `10–20 min` are starting guesses;
  calibrated empirically in Phase 0 against fitness variance (§5).
- **Skill weighting** — are all skill points equal, or weighted by difficulty/profession?
  Equal to start; revisit if one skill dominates the backbone unfairly.
- **Seed count for trust** — how many re-runs before a fitness is "trusted"? (§5)
- **Descriptor bin boundaries** — axes & start-pair are LOCKED (§4); the low/mid/high
  boundaries for the continuous axes are calibrated in Phase 0, and the timing for
  activating axes 3–4 is set as the grid fills.
- **Account provisioning** — auto-create N ServUO accounts on the local shard from the
  kernel so the fleet scales without manual setup.

---

*Kernel is law. Everything above it is fair game for the AI to rewrite — including this
document and the improver itself.*
