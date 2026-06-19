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

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from foundry.kernel.archive import Genome, cell_to_str


def _kill_process_group(proc: "subprocess.Popen") -> None:
    """SIGKILL the child's whole process group. ``claude`` (a Node CLI) spawns
    children that inherit our stdout pipe — killing only the leader leaves the
    pipe open and communicate() blocks forever. Requires start_new_session=True.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


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


# Subject used when folding any uncommitted leftovers the mutator left behind
# into a variant commit. It is bookkeeping, NOT a hypothesis — extraction must
# skip it so the agent's real "foundry-mutation: ..." line is preserved.
_LEFTOVER_COMMIT_SUBJECT = "foundry-mutation: (auto-commit leftover changes)"


def _genome_leftovers(repo: str | Path) -> list[str]:
    """Uncommitted paths that represent the mutator's OWN genome work.

    The leftover fold (``mutate_with_claude``) exists to capture edits the
    mutator made but forgot to commit. The genome body is the ``anima/``
    package and the mutator may only touch tracked files there. But a *reused*
    worktree slot keeps preserved untracked junk around — ``_prepare_worktree``
    runs ``git clean -e data -e .venv``, so prior cycles' untracked, non-ignored
    ``data/`` files (e.g. ``data/cliloc.*.json``) survive into the next cycle.

    Committing that junk as a "leftover" made ``after != before`` even when
    claude changed nothing (timed out / errored before editing), so the cycle
    reported ``changed=True``, burned a full eval window on a parent-identical
    variant, and planted a near-duplicate genome labelled with the placeholder
    hypothesis. Only count REAL mutation content: any tracked change (status
    other than ``??``) or an untracked file under ``anima/``.
    """
    out: list[str] = []
    for line in _git(repo, "status", "--porcelain").stdout.splitlines():
        if not line.strip():
            continue
        status, path = line[:2], line[3:].strip()
        # Rename entries are "R  old -> new"; the new path is what matters.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if status == "??":
            if path.startswith("anima/"):
                out.append(path)
        else:  # tracked modification / addition / deletion / rename
            out.append(path)
    return out


def _commit_all(repo: str | Path, message: str) -> tuple[bool, str]:
    """Stage everything and commit. Returns (committed, head_sha)."""
    if not _git(repo, "status", "--porcelain").stdout.strip():
        return False, head(repo)
    _git(repo, "add", "-A")
    r = _git(repo, "commit", "-m", message)
    return r.returncode == 0, head(repo)


def _extract_hypothesis(repo: str | Path, before: str, after: str) -> str:
    """Read the mutator's hypothesis from the commits it produced.

    The hypothesis is the body of the agent's ``foundry-mutation: <...>``
    commit. If the agent left uncommitted leftovers, ``mutate_with_claude``
    folds them into a *second* commit whose subject is the bookkeeping
    placeholder ``_LEFTOVER_COMMIT_SUBJECT`` — which then becomes HEAD. Reading
    only HEAD would record that placeholder as the hypothesis and lose the
    real one, so scan ``before..after`` (skipping the placeholder).

    The agent is asked for ONE commit, but the budget guidance ("commit early")
    actively invites an early commit followed by a refining one — and the agent
    can iterate (``foundry-mutation: try A`` then ``foundry-mutation: do B``).
    The archived genome IS the final tree (B), so the recorded hypothesis must
    describe B, not the superseded A. Picking the FIRST tagged subject recorded
    the stale intermediate and poisoned the REFLECT log (observe.history feeds
    these back to the mutator). Return the LAST genuine ``foundry-mutation:``
    subject — the one that describes the code that actually gets evaluated.
    """
    subjects = _git(repo, "log", "--reverse", "--pretty=%s",
                    f"{before}..{after}").stdout.splitlines()
    subjects = [s.strip() for s in subjects if s.strip()]
    found = ""
    for subj in subjects:
        if subj == _LEFTOVER_COMMIT_SUBJECT:
            continue
        if "foundry-mutation:" in subj:
            found = subj.split("foundry-mutation:", 1)[-1].strip()
    if found:
        return found
    # No tagged commit (older lineage / hand commit) — fall back to a real
    # subject if any (the most recent real one), else HEAD's subject.
    real = [s for s in subjects if s != _LEFTOVER_COMMIT_SUBJECT]
    if real:
        return real[-1]
    return _git(repo, "log", "-1", "--pretty=%s").stdout.strip()


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
- You have a hard wall-clock budget (~15-20 min) AND a turn budget (~110 tool
  calls). Budget them: skim at most ~5 reference files (wiki pages, actions.md,
  code), then edit and COMMIT EARLY — an uncommitted mutation scores nothing
  and the cycle is wasted. Committing a small change at turn 30 beats running
  out of turns at 60 with uncommitted work.
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
  code) before betting your one mutation on a mechanic. Content map:
  - `mechanics/skill-gain.md` — THE gain law (a skill only gains when the
    task sits between its minSkill and maxSkill — meditating at full mana,
    casting trivially easy spells, or grinding far above the window gains
    NOTHING).
  - `playing/<topic>.md` — 30+ how-to guides for every activity: the exact
    interaction flow for combat-basics, healing, spellcasting, crafting,
    vendors-and-banking, targeting, using-and-training-skills,
    items-and-inventory, hiding-and-stealth, movement-and-travel,
    death-and-resurrection. Read these to learn what a player must DO.
  - `professions/<name>.md` (22 hubs) + `templates/` — what the ideal
    build/loop for a profession looks like; aim your variant at one.
  - `skills/<name>.md` per-skill training notes; `items/weapons|armor|
    tools|reagents.md` equipment facts; `crafting/<trade>.md` full recipe
    tables; `world/minoc.md` (the eval area); `bestiary/` (mob strength).
  Read only the 1–3 pages your hypothesis depends on — your wall-clock
  budget is tight (English pages only; skip ko/ and ja/ mirrors).
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
    stdout = stderr = ""
    timed_out = False
    try:
        # Run claude in its OWN process group so a hung call (e.g. an API stall
        # during a network outage) can be killed group-wide on timeout. With
        # subprocess.run, the timeout kills only the direct child; claude's
        # grandchildren keep the stdout pipe open and communicate() then blocks
        # FOREVER (observed: a 1500s-timeout mutation wedged a slot for 3h).
        proc = subprocess.Popen(
            [
                "claude", "-p", prompt,
                "--model", model,
                "--max-turns", "110",
                "--allowedTools", *_ALLOWED_TOOLS,
            ],
            cwd=str(repo),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # SIGKILL the whole group, then drain (now bounded — the pipe closes
            # once the grandchildren are dead). claude may have committed before
            # hanging; the git check below still picks that up.
            timed_out = True
            _kill_process_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            header = (f"# TIMEOUT after {timeout}s (group killed)\n" if timed_out
                      else f"# exit={proc.returncode}\n")
            Path(log_path).write_text(
                f"{header}## STDOUT\n{stdout}\n## STDERR\n{stderr}\n"
            )
    except FileNotFoundError:
        return MutationResult(False, error="claude CLI not found")

    # Fold the mutator's OWN uncommitted leftovers into a variant commit — but
    # ONLY genome content. Preserved untracked junk from a reused worktree slot
    # (e.g. data/*.json) must not masquerade as a mutation, or a claude run that
    # edited nothing gets scored as a real (parent-identical) variant.
    leftovers = _genome_leftovers(repo)
    if leftovers:
        _git(repo, "add", "--", *leftovers)
        _git(repo, "commit", "-m", _LEFTOVER_COMMIT_SUBJECT)
    after = head(repo)
    if after == before:
        return MutationResult(False, code_ref=after, error="no commit produced")

    hypothesis = _extract_hypothesis(repo, before, after)
    return MutationResult(True, code_ref=after, hypothesis=hypothesis)


def mutate_noop(repo: str | Path, parent: Genome | None) -> MutationResult:
    """No code change — used to test the orchestration loop plumbing."""
    return MutationResult(
        changed=False, code_ref=head(repo),
        hypothesis="noop (plumbing test — re-eval parent code)",
    )
