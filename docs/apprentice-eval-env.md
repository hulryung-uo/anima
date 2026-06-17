# Kernel proposal: a survival/soak evaluation environment

> PROPOSAL for the human kernel owner. `foundry/kernel/` (gm.py, eval.py,
> fitness.py, descriptor.py) is HUMAN-OWNED — agents must not edit it. The
> editable side (`foundry/apprentice.py`) already RUNS long soaks and computes
> autonomy metrics; what's missing is a kernel-side ENVIRONMENT that makes a
> long soak actually exercise survival + death-recovery. Companion to
> `docs/apprentice-track.md` and `docs/kernel-proposals.md`.

## 0. Why this is needed — evidence from 4 soaks (2026-06-16/17)

The 600s scored eval and the current GM arena cannot measure long-horizon
survival. Four warrior soaks (foundry/apprentice.py) showed:

| run | window | outcome |
|---|---|---|
| 240s | survived | no death |
| 2700s (#1) | **died at 33s** | opening fight overwhelmed it → surfaced the death-disconnect bug |
| 1800s→1060s | survived, then idled | wander-loop (now bounded) |
| 2700s→2621s | survived, **12-min idle stall** | nothing to do after clearing mobs |

**Pattern:** the warrior arena spawns **4 HeadlessOne once, no respawn**
(`gm.py:110` `"spawn_mobs": ["HeadlessOne"] * 4`). Death only happens in the
OPENING fight (~1 in 4 runs); if the warrior survives it, the mobs are dead and
there is **nothing to do** — no respawn, and a warrior can't mine — so it idles
(the 729s sample gap). So a long soak is uninformative past the opening, and
death-recovery can't be reliably reproduced or measured.

## 1. What's already in place (reuse — no new work)

- **Threat is real, just finite.** HeadlessOne (HP 16-30) damage the warrior;
  the profile only neutralizes gold (`gm.py:96` `"neutralize": [0x0EED]`), not
  the mobs. The fix is *sustained* threat, not *adding* threat.
- **A healer is reachable.** Soak logs show seek_resurrection pathing to
  "Minoc Healer" at **dist=5** from the workplace (LANE_SPOTS are on the Minoc
  ridge, `gm.py:42-60`). So the agent's OWN resurrection path can work.
- **The death-disconnect blocker is fixed** (commit 95a65dc): the agent no
  longer disconnects when it dies, so seek_resurrection → heal → loot → re-equip
  (Rank 6) can now actually run. → **Self-recovery may already work; measure it
  before building a GM tutor (§5).**
- **Autonomy is already measured** by `foundry/apprentice.py`: deaths,
  self-rescue rate, interventions/hour, longest unassisted stretch,
  alive_fraction over the long horizon.

## 2. Minimal kernel change — sustained threat (the unblocker)

Replace the one-shot 4-mob spawn with a **server-native respawning threat** so
a long soak faces continuous danger and produces repeated death/survival events.

**Preferred — a ServUO Spawner** (auto-respawns, needs no GM presence during the
window): in `gm.fixed_start` (gm.py:564+), for a survival profile, instead of
`command_at("[Add HeadlessOne", ...)` ×4, place and configure a Spawner:

```
[Add Spawner                         # at the workplace tile
[Set SpawnName HeadlessOne           # (or a small mixed list)
[Set Count 3                         # maintain ~3 alive
[Set HomeRange 6                     # within ENGAGE_RANGE so hunt_nearby finds them
[Set MinDelay 00:00:20               # respawn cadence
[Set MaxDelay 00:00:40
```

A new `FIXED_START_PROFILES` entry (kernel) — e.g. `"warrior_survival"` — with a
`"spawner"` spec instead of `"spawn_mobs"`, and `gm.fixed_start` branching on it.
The Spawner must be **WipeNPCs-cleaned between evals** (it already runs `[WipeNPCs`
±12, gm.py:572 — confirm it removes Spawners + their spawn, or add `[RemoveSpawns`).

*Fallback if Spawners are undesirable:* a kernel-side periodic re-spawn — the GM
connection re-issues the spawn command every `respawn_every_s` during the window
(requires keeping the GM active through the window, a bigger change than today's
one-shot `_gm_setup`).

**Difficulty knob:** Count / mob type sets the pressure. Tune so a competent
warrior survives indefinitely but a poor genome dies — that's the gradient the
survival metric needs. Start gentle (Count 3 HeadlessOne) and raise.

## 3. A survival eval MODE (long window, autonomy-scored)

The scored fitness eval must stay as-is (short window, neutralized, comparable —
do NOT pollute it). Add a SEPARATE mode:

- New `EvalConfig` flag (e.g. `mode="survival"`) or a dedicated entrypoint the
  kernel exposes that `foundry/apprentice.py` calls.
- Long window (1800–3600s), sustained-threat profile (§2), **threats NOT
  neutralized**.
- Returns the full trajectory (apprentice.py already analyzes hp_samples for
  death/recovery). Optionally the kernel computes an autonomy summary itself.
- **Hard separation:** survival mode and the scored evolution eval are distinct
  code paths; the GM tutor (§5) lives ONLY in survival mode. This preserves the
  anti-gaming invariant (a rescued agent must never inflate the scored fitness).

## 4. Autonomy fitness term + descriptor (link kernel-proposals P-4)

To let evolution *select* for survival (not just measure it), add
`autonomy_term` to `foundry/kernel/fitness.py` and/or an autonomy axis to
`descriptor.py` — but **measured in a GM-free survival eval** so it rewards real
unassisted survival: `f(survival_under_threat ↑, deaths ↓, self_rescue_rate ↑)`.
Detail in `docs/kernel-proposals.md` P-4; this doc supplies the environment it's
measured in.

## 5. GM tutor (A2) — only as a FALLBACK, after measuring self-recovery

`docs/apprentice-track.md` proposed a GM tutor that resurrects an
unrecoverable agent. **But the disconnect fix may have made it unnecessary:** a
healer is reachable (dist=5) and the agent's seek_resurrection now survives
death. So:

1. **First** run a sustained-threat survival soak (§2-3) and read the autonomy
   metrics. If `self_rescue_rate` is high (the agent walks to the Minoc healer,
   resurrects, re-equips via Rank 6, resumes) → **no GM tutor needed.** The
   shadow-intervention log (already emitted by apprentice.py) tells us how often
   self-recovery fails.
2. **Only if** self-recovery fails often (e.g. healer unreachable from where it
   died, or repeated re-death) → build the A2 GM tutor in the kernel, with the
   two guardrails (separate from scored eval; intervention frequency is a
   measured penalty that must trend → 0, never a goal). Triggers/interventions:
   `docs/apprentice-track.md §4`.

This ordering avoids building kernel machinery (GM tutor) that the editable-side
fix may have already obviated.

## 6. Robustness: eval `_stop` must kill the process group (orphan leak observed)

During the soaks, after the window the eval's `_stop(agent)` did not always kill
the anima client — an orphan kept pinging for minutes (the agent uses
`start_new_session=True`, so SIGINT to the leader pid leaves `uv`→python children
alive). Same class as the mutate-timeout bug fixed editable-side (commit e137364)
and the kernel-proposals review note. **Propose:** `eval.py:_stop` (≈243-256)
should `os.killpg(os.getpgid(pid), …)` the group on the escalation path. Until
then, the editable soak driver should reap orphans (apprentice.py can be extended
to kill leftover `soak*` agents on exit).

## 7. Phasing

- **K1 — sustained threat (§2).** Smallest change that unblocks everything: a
  respawning Spawner + a `warrior_survival` profile. Then a long soak produces
  repeated death/survival events.
- **K2 — survival eval mode (§3).** Formalize the long-window, non-neutralized,
  autonomy-scored path the apprentice driver calls.
- **K3 — measure self-recovery.** Run soaks; read self_rescue_rate /
  interventions-per-hour. Decide if A2 is needed.
- **K4 — autonomy_term/descriptor (§4)** so evolution selects for survival.
- **K5 — GM tutor (§5)** only if K3 shows self-recovery is insufficient.
- **K-robustness — eval `_stop` killpg (§6)** anytime; low-risk, prevents orphans.

## 8. Kernel touch-points (all PROPOSE — do not edit)

| file | change | section |
|---|---|---|
| `foundry/kernel/gm.py` | `warrior_survival` profile + Spawner setup in `fixed_start` (≈564); WipeNPCs covers Spawners (≈572) | §2 |
| `foundry/kernel/eval.py` | survival `mode` (long window, no neutralize, no scored fitness); `_stop` killpg (≈243) | §3, §6 |
| `foundry/kernel/fitness.py` · `descriptor.py` | `autonomy_term` + autonomy descriptor, measured GM-free | §4 |
| (optional) GM tutor | resurrect/kit/safe-move on unrecoverable death, fading + separation guardrails | §5 |

## 9. One-line summary

The editable side is ready (long-soak runner + autonomy metrics + Rank 6 recovery
+ the death-disconnect fix). The one kernel change that unblocks real survival
testing is **sustained (respawning) threat** so a long soak faces continuous
danger instead of clearing 4 mobs and idling. Then measure whether the agent
*self-recovers* (healer is reachable; disconnect is fixed) before deciding the GM
tutor is even needed — and add an `autonomy_term` so evolution can select for
living, not just grinding.
