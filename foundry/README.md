# Anima Foundry

A self-developing agent system: it evolves a population of UO-playing agents
(the `anima/` package is the genome body) by mutating their code, evaluating each
variant against a live ServUO shard, and archiving the best of every behavioral
kind (MAP-Elites). Full design: [`docs/FOUNDRY.md`](../docs/FOUNDRY.md). Architecture diagrams: [`docs/foundry-architecture.md`](../docs/foundry-architecture.md).

## Layout

```
foundry/
  kernel/            HUMAN-OWNED ruler — reverted to a pinned SHA before every eval.
                     MUST NOT import anima/ (the mutator-editable genome body).
    trajectory.py    independent UO packet parser (raw bytes -> TrajectorySummary);
                     parse_file(window_start=...) excludes the GM setup phase
    fitness.py       the fitness scalar (FOUNDRY.md §5): skill-backbone + viability gate
    descriptor.py    the QD behavior axes (FOUNDRY.md §4): profession × sociability …
    archive.py       genome store + MAP-Elites grid + promotion rule
    eval.py          live eval: agent -> uo_proxy -> ServUO; fixed-start via GM;
                     multi-seed averaging (run_eval_multi)
    gm.py            kernel GM driver: minimal wire client that standardizes the
                     eval start (teleport to workplace, pin skill, hand tools);
                     reads the server through its own uo_proxy JSONL (no Huffman)
    provision.py     one-time: create/elevate the foundrygm GameMaster account
    safety.py        kernel revert, kill switch, run guards
  select.py          frontier-biased parent selection      (mutator-editable)
  observe.py         eval evidence assembled for the mutator (mutator-editable)
  mutate.py          LLM mutation operator (claude | noop)  (mutator-editable)
  orchestrator.py    Phase-1 PARALLEL develop loop: K slot worktrees × K accounts
  status.py          archive/grid/lineage view
  selftest.py        offline kernel composition check
```

## Prerequisites (already provisioned on this machine)

- **mono** (runs the .NET ServUO) and **uv** (anima deps) — `brew install mono uv`.
- Local **ServUO** at `~/dev/uo/servuo`, UO client data at `~/dev/uo/uo-resource`.
- `uv sync --all-extras` (aiosqlite/numpy are needed but live under the `llm` extra).
- A `foundrygm` GameMaster account: `python3 -m foundry.kernel.provision --apply`
  (server must be stopped; deliberate human act per FOUNDRY.md §2).

## Run it

```sh
# 1. Boot the local server (listens on 127.0.0.1:2594; precompiled Scripts.dll,
#    the dotnet recompile error on boot is non-fatal).
cd ~/dev/uo/servuo && nohup env MONO_GAC_PREFIX=/opt/homebrew \
    mono ServUO.exe -noconsole > /tmp/servuo.log 2>&1 & disown
#    (nohup+disown: a bare `&` ties the shard to the spawning shell — it
#     dies silently on SIGHUP hours later and every eval starts failing
#     with "agent never entered the world / upstream_connect_failed")

cd ~/dev/uo/anima

# 2. Score a recorded trajectory (offline, no server):
python3 -m foundry.score data/trajectories/*.jsonl
python3 -m foundry.selftest

# 3. One live eval (fixed-start miner: agent is teleported to the Minoc mine
#    with a pickaxe and Mining 35, then scored over the window):
python3 -m foundry.kernel.eval --user probe1 --window 600
#    multi-seed (re-run on fresh accounts, averaged):
python3 -m foundry.kernel.eval --user probe1 --window 600 --seeds 3

# 4. The develop loop. noop backend = plumbing test (no LLM):
python3 -m foundry.orchestrator --cycles 2 --window 120 --backend noop
#    real LLM mutation, 2 cycles in parallel worktrees:
python3 -m foundry.orchestrator --cycles 6 --parallel 2 --window 600 \
        --backend claude --model sonnet

# 5. Watch the grid fill:
python3 -m foundry.status

# 6. Tests:
uv run pytest tests/foundry/ -q
```

Touch `foundry/STOP` to halt an in-progress run.

## How an eval is standardized (anti-variance)

1. The agent logs in on a fresh auto-created account (persona template).
2. The kernel GM session teleports it to its profession's workplace, pins the
   profession skill to a fixed baseline, and hands it tools — all by remote
   serial targeting, so parallel evals never interfere.
3. The agent's first planner tick is held (`--planner-delay`) until setup landed.
4. Only then does the scored window start; `parse_file(window_start=...)` makes
   the setup invisible to fitness (skill set = baseline shift, GM items never
   count as production).

## Status

Phase 0 (kernel + single-cell loop) and Phase 1 (parallel worktree evolution)
are built and running against a live local shard. Next: longer calibration
runs, multi-seed trust thresholds, Phase 2 grid expansion (FOUNDRY.md §11).
