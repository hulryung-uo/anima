# Anima — UO AI Player System

> *Anima (Latin: soul)* — An AI player system that breathes souls into the Ultima Online world.

Anima connects to a UO server (`servuo-rs`) as **real external clients** — the server cannot distinguish AI from human players. Each AI agent has its own personality, daily schedule, memory, and economic role, creating a living world that runs even with zero human players online.

## Architecture

```
┌─────────────────────────────────────────────┐
│              Anima Orchestrator              │
│                                             │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐       │
│  │ Agent 1 │ │ Agent 2 │ │ Agent N │       │
│  │Blacksmth│ │ Merchant│ │   ...   │       │
│  └────┬────┘ └────┬────┘ └────┬────┘       │
│       │            │           │            │
│  ┌────▼────────────▼───────────▼────────┐   │
│  │        Shared Infrastructure         │   │
│  │  LLM (Ollama) │ Memory (SQLite)      │   │
│  └──────────────────────────────────────┘   │
└─────────────────────────────────────────────┘
         │ │ │  (independent TCP connections)
         ▼ ▼ ▼
┌─────────────────────────────────────────────┐
│            servuo-rs Server                 │
│        (Standard UO Packet Protocol)        │
└─────────────────────────────────────────────┘
```

Each agent has 4 layers:

1. **Connection** — TCP socket, UO packet codec, login automation
2. **Perception** — World state tracking (nearby entities, self stats, chat log)
3. **Brain** — 3-tier decision making (90% rules, 8% small LLM, 2% large LLM)
4. **Action** — Movement, combat, speech, trade, skill usage

## Tech Stack

| Area | Technology |
|---|---|
| Language | Python 3.12+ |
| Async | asyncio |
| LLM | Ollama (local, zero cost) |
| Memory | SQLite (aiosqlite) |
| Embedding | Ollama (nomic-embed-text) |
| Config | YAML / Pydantic |
| Dashboard | FastAPI + htmx |

## Project Structure

```
anima/
├── anima/                  # Main package
│   ├── client/             # UO protocol client (TCP, packets, codec)
│   ├── perception/         # World state tracking
│   ├── brain/              # Decision engine (BT + LLM)
│   ├── memory/             # Memory system (SQLite)
│   ├── action/             # Action execution (move, fight, talk, trade)
│   ├── persona/            # Persona definitions & schedules
│   └── orchestrator/       # Multi-agent management
├── personas/               # YAML persona files
├── data/                   # Maps, world knowledge
├── docs/                   # Design & analysis documents
└── tests/                  # Tests
```

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)
- [Ollama](https://ollama.com/) (local LLM)
- A running [servuo-rs](https://github.com/servuo-rs) server

## Quick Start

```bash
# Install dependencies
uv sync

# Start Ollama with required models
ollama pull gemma3:4b
ollama pull llama3.1:8b
ollama pull nomic-embed-text

# Run a single agent
uv run python -m anima --host 127.0.0.1 --port 2593 --user admin --pass admin
```

## Documentation

- [DESIGN.md](DESIGN.md) — Full system design document
- [docs/classicuo-analysis.md](docs/classicuo-analysis.md) — ClassicUO protocol analysis
- [docs/implementation-plan.md](docs/implementation-plan.md) — Implementation plan

## Development Status

**Phase 0: Foundation** — In progress

See [DESIGN.md § Development Roadmap](DESIGN.md#6-development-roadmap) for the full plan.
