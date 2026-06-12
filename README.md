# Anima

> *Anima (Latin: soul)* — What if AI characters actually lived in Britannia?

[![Fork this repo](https://img.shields.io/github/forks/hulryung-uo/anima?style=social)](https://github.com/hulryung-uo/anima/fork)

## 🧬 Anima Foundry — Evolution, Not Just Automation

Anima started as an AI that plays Ultima Online. It grew into something stranger: **Anima Foundry**, a system where an AI *develops* AI players — by mutating their code, evaluating every variant against a live server, and keeping the best of every behavioral kind.

```
            ┌──────────────────────────────────────────────────┐
            │                  FOUNDRY LOOP                    │
            │                                                  │
   select ──►  pick a parent elite + an empty target cell      │
            │       (MAP-Elites grid: profession × sociability)│
   mutate ──►  Claude reads the eval evidence + the UO wiki,   │
            │  forms ONE hypothesis, edits the agent's code    │
   evaluate ►  the variant plays 600s on a live ServUO shard   │
            │  (fresh character, GM-standardized start,        │
            │   3 parallel seeds, isolated lane workplaces)    │
   score  ──►  a HUMAN-OWNED kernel parses the raw packet      │
            │  stream independently and computes fitness       │
   archive ─►  better than the cell's elite? it takes the cell │
            └──────────────────────────────────────────────────┘
```

The grid is currently **21/21 full**: every profession (gathering, crafting, combat, magic, bard, stealth, none) × every sociability level (silent, occasional, chatty) has a code-evolved champion. Highlights from the lineage so far:

- A mage that **speaks once per spell practice** beat its silent ancestor — the mutation that finally completed the MAGIC row at fitness 168 (held-out validated: it replicates at 0.93).
- A mutation **removed Mining from the character creation template** to crack the CRAFTING cell — evolution discovered the birth-skill lever on its own.
- A thief variant that overshot its sociability target accidentally became the best chatty miner. Serendipity is kept: MAP-Elites archives what *landed*, not what was aimed.
- Evolution also found a reward hack within hours of a scoring bug going live (re-crediting bouncing item stacks). The kernel's held-out re-evaluation caught it, five inflated genomes were demoted with evidence preserved, and the ruler was fixed the same day.

**This isn't AGI. It's not even close.** It's a Darwin-Gödel-style loop wired to a 1997 MMO. But watching a population of game bots *evolve real code changes* — new procedures, new pacing, new social behavior — because the fitness function rewarded it, is genuinely fun.

## Why This Exists

I've been playing Ultima Online since 1998. Almost thirty years later, I still think it's the greatest game ever made. Nothing else has come close to that feeling — a true sandbox where anything could happen, where the world felt alive because *real people* made it alive.

But time passes. The free shards I chased for nostalgia felt empty — a few min-maxed veterans, towns that were once bustling marketplaces sitting silent. The magic was gone, not because the game changed, but because the *people* did.

Then I had a thought: **what if, instead of macros and bots, actual AI characters could live in Britannia?** Not scripted NPCs. Real characters — ones that walk to work, dig ore, panic when a PK shows up, gossip about it, and come home with stories. And if the characters are software… they can *get better at living* the same way software does: by changing their own code.

## The Two Halves

### 1. Anima — the avatar (the genome's body)

Anima connects to a UO server as a **real game client** over the standard packet protocol. From the server's perspective, an agent is indistinguishable from a human player on ClassicUO. No server modifications, no special privileges — just a soul in a body.

- **Eyes** — `anima/perception/`: packet handlers maintain `WorldState` (entities, items, terrain, journal, own hidden/war flags). The brain never parses bytes.
- **Legs** — A* pathfinding on real UO map data, Z-aware walkability, door traversal, resync recovery.
- **Hands** — `anima/procedures/` (18 procedures): mining, smelting, batch blacksmithing (craft-gump MAKE LAST loop), melee combat with bandage interleave + shield parry + corpse looting, magery with meditation fallback, hiding with stealth-walking, peacemaking, vendor buy/sell, banking.
- **A brain** — `anima/planner/`: a priority-rule planner picks procedures per persona profession (`PROFESSION_LOOPS`); LLM escalation is optional and off during evals.
- **A mouth** — speaks in-game, responds in character; sociability is a measured, evolved behavior axis.

### 2. Foundry — the developer (the genome's editor)

- **`foundry/kernel/` is HUMAN-OWNED.** It is pinned to a git SHA and reverted before every eval. It parses the wire traffic *independently of the agent* (a proxy logs every packet), so a genome cannot lie about its own performance. Mutating agents never edit the kernel; the kernel never imports agent code.
- **Fitness** = skill-gain/hour (backbone) + 0.3×gold/hour + 0.2×produced-value/hour, gated by survival/liveness/anti-stuck. All measured from server packets only.
- **Descriptor** = profession (from which skill categories actually gained) × sociability (speech share of actions). New axes (aggression, mobility) are staged for Phase 2.
- **Anti-variance**: every eval is a *fresh character*, teleported by a kernel GM session to an isolated lane workplace (10 map-scanned, flood-fill-verified spots), skills pinned to a fixed baseline, tools provided, starting gold neutralized. Multi-seed evals run concurrently and average.
- **Anti-gaming**: scored windows exclude the GM setup; produce credit is per-item-serial amount *delta* (stack bounces mint nothing); champions get **held-out re-evaluation** on fresh accounts — genomes that don't replicate are demoted by a human, with the evidence preserved in their records.
- **REFLECT**: every mutation's hypothesis and outcome feeds back into the next mutation prompt, along with the current elites' proven recipes — evolution remembers what worked and stops re-walking dead ends.

### The knowledge flywheel

Agents and operators share a companion wiki (835+ pages, source-verified against the server code). The mutator **reads it** before betting its one mutation on a game mechanic — and **files discrepancy reports back** when live evidence contradicts a page. Verified field discoveries (skill lockout timings, crafting failure costs, drop-bounce rules) get written into the wiki, which future mutations then read. The wiki is the system's long-term memory.

## Current Personas

| Persona | Name | Focus | Eval role |
|---------|------|-------|-----------|
| Adventurer | Anima | Melee combat, exploring | COMBAT row (warrior arena) |
| Blacksmith | Tormund | Smithing at the forge | CRAFTING row |
| Miner | Grimm | Mining, smelting | GATHERING row |
| Mage | Elric | Magery, meditation | MAGIC row |
| Bard | Melody | Music, peacemaking | BARD-SOCIAL row |
| Thief | Shade | Hiding, stealth | THIEF-STEALTH row |
| Woodcutter | Bjorn | Lumberjacking, carpentry | free play |
| Merchant | Sera | Trading, tailoring | free play |
| Ranger | Ash | Archery, hunting | free play |

## Getting Started

### Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- A UO server — for the Foundry loop you want a **local [ServUO](https://www.servuo.com/)** shard
- UO client data files (map0, statics0, tiledata)
- Optional: an LLM provider for free-play Think escalation ([Ollama](https://ollama.com/) local, or an API)
- For evolution runs: [Claude Code](https://claude.com/claude-code) (the mutation operator shells out to it)

### Setup

```bash
git clone https://github.com/hulryung-uo/anima.git
cd anima
uv sync --all-extras
cp config.example.yaml config.yaml   # server, account, LLM settings
```

### Run a single agent (free play)

```bash
uv run python -m anima                 # plays per its persona
uv run python -m anima --tui          # with the in-process terminal dashboard
uv run python tools/tui.py            # or a standalone TUI in another terminal
```

A live **web dashboard** ships with the agent (`--web-port`, default 8160): canvas minimap with movement trail, session skill deltas, merged activity/journal feed, multi-slot tabs for watching parallel agents.

### Run evolution (Foundry)

```bash
# 1. Boot the local shard (listens on 127.0.0.1:2594)
cd ~/dev/uo/servuo && MONO_GAC_PREFIX=/opt/homebrew mono ServUO.exe -noconsole &
cd ~/dev/uo/anima

# 2. One-time: create the kernel's GameMaster account (server stopped; human act)
python3 -m foundry.kernel.provision --apply

# 3. One live eval — fixed-start, scored window, no mutation
uv run python -m foundry.kernel.eval --user probe1 --window 600 --seeds 3

# 4. The evolution loop: 10 cycles, 3 parallel slots, 3 seeds each
uv run python -m foundry.orchestrator --cycles 10 --parallel 3 --seeds 3 \
        --window 600 --backend claude --model sonnet

# 5. Plant a HEAD root genome (surface new base-code capabilities to old lineages)
uv run python -m foundry.orchestrator --cycles 0 --seed \
        --persona blacksmith --fixed-start crafter --seeds 3 --window 600

# 6. Watch / verify / stop
uv run python -m foundry.status                  # grid + lineage
uv run python -m foundry.reeval g_00039          # held-out champion check
touch foundry/STOP                               # graceful halt
```

Full runbook: [`foundry/README.md`](foundry/README.md). Design: [`docs/FOUNDRY.md`](docs/FOUNDRY.md).

### Development

```bash
uv run pytest          # run tests   (tests/foundry/ = kernel invariants)
uv run ruff check      # lint
uv run ruff format     # format
```

## Documentation

- [docs/FOUNDRY.md](docs/FOUNDRY.md) — **the Foundry design**: evolution loop, trusted kernel, MAP-Elites grid, locked fitness/descriptor
- [foundry/README.md](foundry/README.md) — Foundry runbook (boot, eval, runs, anti-variance protocol)
- [docs/actions.md](docs/actions.md) — catalog of every action primitive and procedure
- [DESIGN.md](DESIGN.md) — original architecture and system design
- [docs/wiki-integration.md](docs/wiki-integration.md) — the knowledge flywheel (wiki tools, report rules)
- [docs/reinforcement-learning.md](docs/reinforcement-learning.md) — RL methodology notes

## ⚠️ Security Notice — Please Read Before Using

Anima is a **hobby/research project**, not a hardened production tool. A few things to keep in mind before you clone, fork, or run it:

### Your credentials live in `config.yaml` (plaintext)

- `config.yaml` is **gitignored** by default — do **not** remove it from `.gitignore` and do **not** commit the file.
- The UO login protocol sends usernames and passwords over the wire in plaintext (no TLS). Treat any account you use with Anima as **disposable**: generate a fresh account per shard, never reuse a password you care about.
- API keys sit in the same file. Rotate them if you ever share your machine or suspect a leak.

### This repo's git history contains an old test account

Early in development (March 2026, commits `ded3d96` – `1ad584c`), a `config.yaml` with placeholder credentials `test5 / test5` for `uo.hulryung.com:2593` was briefly committed before being gitignored. Those credentials are still visible in the git history. The account has been invalidated, but if you mirror or fork this repo, **do not assume old history is clean**.

### Evolution edits and commits code automatically

A Foundry run calls Claude Code to **edit the agent source in isolated worktrees and commit** under your git identity (it does not push). The legacy supervisor (`tools/supervisor.py --claude`) goes further and **pushes to `origin`**. Run either on a dedicated branch or fork you're OK having rewritten — never on a branch you share.

### The shared test server is a toy

`uo.hulryung.com:2593` is a ServUO shard kept running for experimentation — not monitored, not persistent, not production. For serious work (and for any Foundry run), spin up your own local shard.

**TL;DR — treat Anima like any game-bot demo off GitHub: fun to play with, not something to point at your main account or a server you care about.**

## Join In

This is an experiment. It might go somewhere interesting, or it might just be a really elaborate way to evolve virtual blacksmiths. Either way, it's fun.

[**→ Fork this repo**](https://github.com/hulryung-uo/anima/fork) — spin up your own shard, seed a population, and watch the grid fill.

If you have ideas, questions, or just want to see what the agents are up to — open an issue or drop by.

## License

This is a personal project. Do whatever you want with it.
