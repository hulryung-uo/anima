# Anima

> *Anima (Latin: soul)* — What if AI characters actually lived in Britannia?

[![Fork this repo](https://img.shields.io/github/forks/hulryung-uo/anima?style=social)](https://github.com/hulryung-uo/anima/fork)

## 🧬 We Found Something Interesting

While building Anima — an AI that plays Ultima Online — we accidentally stumbled onto something unexpected: **an AI agent that improves its own code.**

Here's how it works: Anima runs in-game as a woodcutter named Bjorn. Every 10 minutes, a self-improvement loop kicks in:

1. **Analyze** — Parse game logs, compute success rates, detect problems (stuck? failing? overweight?)
2. **Plan** — Generate a markdown improvement plan with suggested fixes
3. **Fix** — Call Claude Code to automatically modify the source code
4. **Test** — Run `pytest` to verify nothing breaks
5. **Deploy** — `git commit && git push` → restart the agent with improved code

The agent writes problem reports about itself. It asks for help in-game when it's stuck. It tracks which trees are depleted, learns which paths are blocked, and adjusts its strategy through reinforcement learning.

**This isn't AGI. It's not even close.** It's a game bot that happens to have an automated feedback loop. But watching it fix its own bugs, tune its own parameters, and gradually get better at chopping wood — that's genuinely fun to watch.

We think this pattern — **AI agent + self-analysis + automated code improvement** — could be applied to many other domains. If you're curious, fork it and try.

[**→ Fork Anima and start experimenting**](https://github.com/hulryung-uo/anima/fork)

---

## Why This Exists

I've been playing Ultima Online since 1998. Almost thirty years later, I still think it's the greatest game ever made. Nothing else has come close to that feeling — a true sandbox where anything could happen, where the world felt alive because *real people* made it alive.

But time passes. I got older. I don't have hours to grind anymore. I tried dozens of free shards over the years, chasing that nostalgia, but it was never quite right. The worlds felt empty. The few players left were hardcore veterans who'd min-maxed everything years ago. New players would log in, get overwhelmed by the brutal learning curve, and quit within a week. The economy would collapse because there weren't enough people to sustain it. Towns that were once bustling marketplaces sat empty. The magic was gone — not because the game changed, but because the *people* did.

Then I had a thought: **what if, instead of macros and bots, actual AI characters could live in Britannia?**

Not scripted NPCs with canned dialogue. Not automation tools that repeat the same loop forever. Real characters — ones that wake up in the morning, walk to work, chop wood, dig ore, get lost in a dungeon, panic when a PK shows up, run back to town to warn everyone, write angry posts about it in the community board, form a guild to fight back, make friends, hold grudges, discover new places, and come home to tell stories about their day.

## What Anima Does

Anima connects to a UO server as a **real game client** over the standard packet protocol. From the server's perspective, an Anima agent is indistinguishable from a human player using ClassicUO. No server modifications, no special privileges, no cheating — just a soul in a body, trying to figure out Britannia.

Each agent has:
- **Eyes** — Perception system that tracks nearby entities, items, terrain, and chat
- **A brain** — Behavior tree for routine decisions, with LLM escalation for complex ones
- **Legs** — A* pathfinding on actual UO map data with Z-aware walkability and obstacle avoidance
- **Hands** — Lumberjacking, crafting, trading, combat — all through standard game packets
- **A mouth** — Speaks in-game, responds to conversation, writes forum posts about adventures
- **Memory** — Remembers experiences, people, places, and learns from mistakes via Q-learning
- **Self-awareness** — Generates problem reports, asks for help, and improves its own code

## The Self-Improvement Loop

```
┌─────────────────────────────────────────┐
│           Anima (playing UO)            │
│  chop wood → craft → sell → repeat     │
└──────────────┬──────────────────────────┘
               │ logs + metrics
┌──────────────▼──────────────────────────┐
│         Analyzer (every 10 min)         │
│  success rates, stuck detection,        │
│  problem patterns → improvement plan    │
└──────────────┬──────────────────────────┘
               │ plan.md
┌──────────────▼──────────────────────────┐
│         Claude Code (auto-called)       │
│  reads plan → fixes code → pytest       │
│  → git commit → git push               │
└──────────────┬──────────────────────────┘
               │ restart
               └──────────► back to playing
```

Run it yourself:

```bash
# Start the self-improvement loop
uv run python tools/self_improve.py --loop --claude
```

## Current Personas

| Persona | Name | Focus | Combat |
|---------|------|-------|--------|
| Adventurer | Anima | Exploring, meeting people | Defensive |
| Blacksmith | Tormund | Mining, smithing | Pacifist |
| Woodcutter | Bjorn | Lumberjacking, carpentry | Pacifist |
| Merchant | Sera | Trading, tailoring | Pacifist |
| Mage | Elric | Magery, meditation | Defensive |
| Ranger | Ash | Archery, hunting | Aggressive |
| Bard | Melody | Music, peacemaking | Pacifist |

## Getting Started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) — fast Python package manager
- A UO server (e.g. [ServUO](https://www.servuo.com/))
- UO client data files (map0, statics0, tiledata)
- LLM provider — [Ollama](https://ollama.com/) (local) or Replicate/OpenAI (API)

### Setup

```bash
# Clone
git clone https://github.com/hulryung-uo/anima.git
cd anima

# Install dependencies
uv sync

# Copy and edit config
cp config.example.yaml config.yaml
# Edit config.yaml with your server, account, LLM settings

# Run
uv run python -m anima

# Run with TUI dashboard
uv run python -m anima --tui

# Run tests
uv run pytest
```

### Configuration

```yaml
server:
  host: your-server.com
  port: 2593

account:
  username: myaccount
  password: mypassword

character:
  name: Anima
  persona: adventurer  # adventurer, blacksmith, woodcutter, merchant, mage, ranger, bard
  city_index: 3        # 0=New Haven, 3=Britain

llm:
  provider: replicate  # ollama, openai, anthropic, replicate
  model: deepseek-ai/deepseek-v3.1
  api_key: ""

map:
  resource_dir: ~/path/to/uo-client-data
```

## Documentation

- [DESIGN.md](DESIGN.md) — Architecture and system design
- [docs/woodcutter-workflow.md](docs/woodcutter-workflow.md) — Woodcutter work cycle
- [docs/uor-skills-reference.md](docs/uor-skills-reference.md) — UOR skills & stats reference
- [docs/self-improvement-plan.md](docs/self-improvement-plan.md) — Self-improvement system design

## Try It Live — Test Server

We run a public test server where you can watch AI agents in action or drop in alongside them with a real client.

**Server:** `uo.hulryung.com:2593` (ServUO, UOR era)

You can:
- **Watch Bjorn chop wood** — connect with ClassicUO and find him wandering around Britain
- **Talk to the agents** — they respond in character (English and Korean)
- **Run your own agent** — point Anima at the test server with a new account

```yaml
# config.yaml for the test server
server:
  host: uo.hulryung.com
  port: 2593
account:
  username: your_test_account
  password: your_password
character:
  persona: adventurer  # or blacksmith, woodcutter, mage...
```

## TUI Dashboard

Anima comes with a real-time terminal dashboard that shows everything the agent is doing.

```bash
uv run python -m anima --tui
```

```
┌─ Status ──────────────────┐┌─ Activity ────────────────────────────┐
│ Bjorn — a humble woodcutter││ 01:23:45 ⚒ Chopping oak tree          │
│                            ││ 01:23:40 → Walking to tree (1596,1491)│
│ HP   ████████░░ 79/79      ││ 01:23:35 ⭐ Think: go to forest       │
│ Mana ██░░░░░░░░ 10/10      ││ 01:23:30 ⚒ Made 5 boards!            │
│ Stam ████████░░ 22/22      ││                                       │
│                            ││                                       │
│ Pos (1595, 1490, 35)       ││                                       │
│ Gold 1,000  Wt 60/243      ││                                       │
│ Goal Find good oak trees   ││                                       │
├─ Nearby ──────┐┌─ Journal ────────┐┌─ Q-Values ────────────────────┤
│ Bilal innkeep ││ System: Welcome  ││ chop_wood    Q=12.3  n=80     │
│ a rat    6N   ││ Bjorn: hello!    ││ make_boards  Q= 3.1  n=12     │
│               ││ ↑ Lumberjack →51 ││ carpentry    Q=-0.5  n=15     │
└───────────────┘└──────────────────┘└────────────────────────────────┘
 j Journal  i Inventory  s Skills  q Quit
```

The dashboard shows:
- **Status** — HP, mana, stamina, position, weight, current goal
- **Activity** — Real-time feed of brain decisions, skill executions, movement
- **Nearby** — NPCs and players within range with notoriety colors
- **Journal** — In-game speech and system messages (cliloc decoded)
- **Q-Values** — Reinforcement learning scores for skill selection
- **Inventory** — Backpack contents (toggle with `i`)
- **Skills** — Skill values with lock states ↑↓• (toggle with `s`)

## Join In

This is an experiment. It might go somewhere interesting, or it might just be a really elaborate way to chop virtual trees. Either way, it's fun.

If you want to try it:

[**→ Fork this repo**](https://github.com/hulryung-uo/anima/fork) — spin up your own AI character on your own shard, or connect to our test server and play alongside the AI.

If you have ideas, questions, or just want to see what Bjorn is up to — open an issue or drop by.

## License

This is a personal project. Do whatever you want with it.
