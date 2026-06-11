"""Mutation operator (FOUNDRY.md §6) — mutator-editable.

This is the "AI develops AI" step: given a parent genome and its eval
observation, an LLM (Claude Code, headless) edits the anima/ genome body to
either improve fitness in its current cell or change behavior to reach an empty
cell, then commits the variant. The kernel is reverted afterward by the
orchestrator so the mutation can never include a changed ruler.

Backends:
  - "claude" : real LLM mutation via `claude -p` (the develop cycle proper).
  - "noop"   : no code change (re-eval parent) — for testing loop plumbing
               without spending an LLM call.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from foundry.kernel.archive import Genome, cell_to_str


@dataclass
class MutationResult:
    changed: bool
    code_ref: str = ""
    hypothesis: str = ""
    error: str = ""


def _git(repo: str | Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, timeout=timeout)


def head(repo: str | Path) -> str:
    r = _git(repo, "rev-parse", "HEAD")
    return r.stdout.strip()


def _commit_all(repo: str | Path, message: str) -> tuple[bool, str]:
    """Stage everything and commit. Returns (committed, head_sha)."""
    if not _git(repo, "status", "--porcelain").stdout.strip():
        return False, head(repo)
    _git(repo, "add", "-A")
    r = _git(repo, "commit", "-m", message)
    return r.returncode == 0, head(repo)


_MUTATION_PROMPT = """You are the mutation operator of Anima Foundry — an evolutionary
system that develops a UO-playing agent. The agent's code is the `anima/` package
(the "genome body"). You are in an isolated git worktree checked out at the parent
genome's code. You make ONE focused change to improve it, then commit.

## The agent you are mutating
{observation}

## How the agent is evaluated (so you know what matters)
The agent plays live for a fixed window. It starts AT its workplace with its tool
and its profession skill pinned (e.g. miner: at the Minoc mine, pickaxe in pack,
Mining 35). Fitness = viability_gate × (skill_gain_rate + 0.3·gold_rate +
0.2·produce_rate + behavior_bonus). The backbone is server-confirmed SKILL GAIN
PER HOUR doing its trade. Dying, freezing, or walking into walls gates everything
toward zero.

Every eval is a NEWLY CREATED character on a fresh account — nothing persists
between evals. That makes the creation template part of the genome: editing
PERSONA_SKILLS / PERSONA_STATS in `anima/client/appearance.py` changes what the
agent is BORN with, and ServUO grants starter items per creation skill (e.g.
Blacksmith → smith tools + ingots; Magery → spellbook + reagents). Changing
birth skills is often the cheapest way to shift profession_focus — far cheaper
than coding an in-game acquisition loop. (Creation rules: ≤4 skills, values
0-50 summing to exactly 100 or 120; stats sum to exactly 90.)

## Your goal this cycle
{goal}

## Hard rules
- Edit ONLY files under `anima/`. NEVER touch `foundry/kernel/` (the fitness ruler —
  editing it is cheating and will be reverted anyway).
- Make ONE focused, minimal change tied to a clear hypothesis. Do not refactor.
- You have a hard wall-clock budget (~15-20 min). Commit a small working change
  EARLY rather than perfecting a large one — an uncommitted mutation scores
  nothing and the cycle is wasted.
- The agent is driven by the v2 rule-based planner: `anima/planner/planner.py`
  (priority selection) and `anima/procedures/*.py` (mine/smelt/craft/sell/etc).
  Movement is `anima/action/movement.py`. Persona templates:
  `anima/client/appearance.py` (PERSONA_SKILLS/PERSONA_STATS) + `personas/*.yaml`.
- BEFORE coding, read `docs/actions.md` IF PRESENT in this worktree — the
  authoritative catalog of every action primitive and procedure (signatures,
  preconditions, failure modes, freeze traps). When it exists, import
  primitives from the `anima.actions` façade instead of re-implementing
  packet flows. (Older lineages predate the doc — skip this if absent.)
- UO game mechanics are NOT guessable — check the wiki at
  `~/dev/uo/uowiki/src/content/docs/` (source-verified against THIS server's
  code) before betting your one mutation on a mechanic. Highest value:
  `mechanics/skill-gain.md` (a skill only gains when the task sits between
  its minSkill and maxSkill — meditating at full mana, casting trivially easy
  spells, or working far above the task's window gains NOTHING),
  `skills/<name>.md` per-skill training notes, `world/minoc.md` (the eval
  area: what exists near the workplace), `bestiary/` (mob strength),
  `templates/` (profession builds). Read only the 1–3 pages your hypothesis
  depends on — your wall-clock budget is tight.
- If the parent's eval evidence CONTRADICTS a wiki page (wrong number, missing
  behavior), file a discrepancy report — the librarian triages them daily:
      uv run python tools/wiki_report.py --agent foundry-mutator \
        --wiki-root ~/dev/uo/uowiki --page <src/content/docs/...> \
        --claim "..." --observed "..." --expected "..." --evidence "..."
  Never edit the wiki directly, never pass --commit. (Skip if the script is
  absent in this worktree — older lineages predate it.)
- Run `uv run --all-extras pytest tests/ -q -x --ignore=tests/foundry` —
  only commit if green.
- Commit ALL your changes with EXACTLY this subject form so the system can read
  your hypothesis:
      foundry-mutation: <one-line hypothesis of what this change improves>
- Do not push. Do not edit foundry/. One commit.
"""

# Tool allowlist for the headless mutator: edit/search freely, but Bash is
# scoped to test-running and committing. (No push, no network, no rm.)
_ALLOWED_TOOLS = [
    "Edit", "Write", "MultiEdit", "Read", "Glob", "Grep",
    "Bash(git add:*)", "Bash(git commit:*)", "Bash(git status:*)",
    "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(uv run:*)",   # pytest / ruff / python, with flags like --all-extras
]


def mutate_with_claude(
    repo: str | Path,
    observation: str,
    parent: Genome | None,
    target_cell: tuple | None,
    timeout: int = 900,
    model: str = "sonnet",
    log_path: str | Path | None = None,
    eval_setup: str = "",
) -> MutationResult:
    """Invoke headless Claude Code to mutate anima/ and commit one variant."""
    if target_cell is not None:
        goal = (
            f"EXPLORE: change the agent's behavior so it lands in the empty cell "
            f"`{cell_to_str(target_cell)}` (profession|sociability-bin). E.g. make it "
            f"practice a different skill category, or talk more/less."
        )
    else:
        cur = cell_to_str(parent.cell) if parent else "?"
        goal = (
            f"IMPROVE: raise the fitness of this agent within its current cell `{cur}`. "
            f"Focus on the biggest limiter in the observation above."
        )
    if eval_setup:
        goal += (
            f"\n\nYour variant will be evaluated as: {eval_setup}. Mutate the code "
            f"paths THAT persona actually executes (its profession loop / planner "
            f"priorities) — changes to other professions' loops never run."
        )
    prompt = _MUTATION_PROMPT.format(observation=observation, goal=goal)

    before = head(repo)
    try:
        proc = subprocess.run(
            [
                "claude", "-p", prompt,
                "--model", model,
                "--max-turns", "60",
                "--allowedTools", *_ALLOWED_TOOLS,
            ],
            cwd=str(repo),
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text(
                f"# exit={proc.returncode}\n## STDOUT\n{proc.stdout}\n"
                f"## STDERR\n{proc.stderr}\n"
            )
    except FileNotFoundError:
        return MutationResult(False, error="claude CLI not found")
    except subprocess.TimeoutExpired:
        # claude may have committed before timing out; fall through to check git.
        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text(f"# TIMEOUT after {timeout}s\n")

    # fold any uncommitted leftovers into a variant commit
    _commit_all(repo, "foundry-mutation: (auto-commit leftover changes)")
    after = head(repo)
    if after == before:
        return MutationResult(False, code_ref=after, error="no commit produced")

    subj = _git(repo, "log", "-1", "--pretty=%s").stdout.strip()
    hypothesis = (subj.split("foundry-mutation:", 1)[-1].strip()
                  if "foundry-mutation:" in subj else subj)
    return MutationResult(True, code_ref=after, hypothesis=hypothesis)


def mutate_noop(repo: str | Path, parent: Genome | None) -> MutationResult:
    """No code change — used to test the orchestration loop plumbing."""
    return MutationResult(
        changed=False, code_ref=head(repo),
        hypothesis="noop (plumbing test — re-eval parent code)",
    )
