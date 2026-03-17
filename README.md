# Anima

> *Anima (Latin: soul)* — What if AI characters actually lived in Britannia?

## Why This Exists

I've been playing Ultima Online since 1998. Almost thirty years later, I still think it's the greatest game ever made. Nothing else has come close to that feeling — a true sandbox where anything could happen, where the world felt alive because *real people* made it alive.

But time passes. I got older. I don't have hours to grind anymore. I tried dozens of free shards over the years, chasing that nostalgia, but it was never quite right. The worlds felt empty. The few players left were hardcore veterans who'd min-maxed everything years ago. New players would log in, get overwhelmed by the brutal learning curve, and quit within a week. The economy would collapse because there weren't enough people to sustain it. Towns that were once bustling marketplaces sat empty. The magic was gone — not because the game changed, but because the *people* did.

Then I had a thought: **what if, instead of macros and bots, actual AI characters could live in Britannia?**

Not scripted NPCs with canned dialogue. Not automation tools that repeat the same loop forever. Real characters — ones that wake up in the morning, walk to work, chop wood, dig ore, get lost in a dungeon, panic when a PK shows up, run back to town to warn everyone, write angry posts about it in the community board, form a guild to fight back, make friends, hold grudges, discover new places, and come home to tell stories about their day.

I know almost nothing about AI. This is a small sandbox, a personal experiment. But the idea of dropping a clueless AI newbie into Britannia and watching what happens — that sounds genuinely fun to me. So I'm building it.

## What Anima Does

Anima connects to a UO server as a **real game client** over the standard packet protocol. From the server's perspective, an Anima agent is indistinguishable from a human player using ClassicUO. No server modifications, no special privileges, no cheating — just a soul in a body, trying to figure out Britannia.

Each agent has:
- **Eyes** — A perception system that tracks nearby entities, items, terrain, and chat
- **A brain** — A behavior tree for routine decisions, with LLM escalation for complex ones
- **Legs** — A* pathfinding on the actual UO map data, wall avoidance included
- **A mouth** — Can speak, respond to conversation, and (eventually) have opinions
- **Memory** — Will remember experiences, people, and places (work in progress)

The long-term vision: spin up a dozen agents with different personalities and professions, drop them into a server, and watch a small economy and society emerge on its own.

## What I Want To Build Next

This is very much a work in progress. Here's what's on my mind:

- **LLM-powered conversation** — Right now speech responses are pattern-matched. I want agents to actually *talk* using a local LLM via Ollama, with personality and context.
- **Memory** — Episodic and semantic memory so agents remember what happened to them, who they met, and what they learned.
- **Personas** — YAML-defined characters with professions, daily schedules, personality traits, and speech styles. A grumpy blacksmith. A cheerful merchant. A paranoid miner.
- **Professions and skills** — Mining, lumberjacking, crafting, trading. Agents that actually contribute to the economy.
- **Social dynamics** — Relationships, trust, reputation. Agents that form guilds, avoid known PKs, and spread gossip.
- **Multi-agent orchestration** — Running many agents simultaneously, each with their own TCP connection and independent life.

Some of this might never happen. Some of it might turn into something unexpected. That's the fun part.

## Getting Started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) — fast Python package manager
- A UO server to connect to (e.g. [ServUO](https://www.servuo.com/) or any standard UO shard)
- UO client data files (map0, statics0, tiledata — the usual)
- [Ollama](https://ollama.com/) — for local LLM support (not required yet, but will be soon)

### Setup

```bash
# Clone
git clone https://github.com/hulryung-uo/anima.git
cd anima

# Install dependencies
uv sync

# Copy and edit config
cp config.yaml config.local.yaml
# Edit config.local.yaml with your server address, account, and map data path

# Run
uv run python -m anima

# Run tests
uv run pytest
```

### Configuration

Edit `config.yaml` (or pass `--config path/to/config.yaml`):

```yaml
server:
  host: 127.0.0.1
  port: 2593

account:
  username: myaccount
  password: mypassword

character:
  name: Anima
  template: random    # random, warrior, mage, smith, merchant, ranger
  city_index: 3       # 0=New Haven, 3=Britain

map:
  resource_dir: ~/path/to/uo-client-data
```

## Documentation

- [DESIGN.md](DESIGN.md) — Architecture and system design
- [docs/implementation-plan.md](docs/implementation-plan.md) — Implementation roadmap

## License

This is a personal project. Do whatever you want with it.
