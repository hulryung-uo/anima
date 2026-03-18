# CLAUDE.md — Agent Work Rules

## Project Overview

Anima is a Python-based AI player system for Ultima Online. It connects to `servuo-rs` as an external client using the standard UO packet protocol.

## Key References

- `DESIGN.md` — Full system design (architecture, roadmap, tech stack)
- `docs/classicuo-analysis.md` — ClassicUO protocol analysis (packet handlers, entity model, all subsystems)
- `docs/implementation-plan.md` — Concrete implementation plan (module mapping, code sketches)
- `docs/skill-system.md` — Skill system design (skill catalog, packet requirements, file structure)
- `docs/reinforcement-learning.md` — **RL 학습 방법론** (Q-learning, UCB1, state encoding, reward signals, LLM 연동)
- ClassicUO source: `~/dev/uo/classicuo/` (C# reference client)
- servuo-rs source: `~/dev/uo/servuo-rs/` (Rust server, the target server)

## Code Conventions

- Python 3.12+, use modern syntax (type hints, `dict[K,V]`, `list[T]`, `X | None`)
- Async-first: use `asyncio` for all I/O (TCP, SQLite, HTTP)
- Use `dataclass` or `pydantic.BaseModel` for data structures
- Use `structlog` for logging
- Use `struct` module for binary packet encoding/decoding (Big-Endian)
- Persona definitions in YAML, config in YAML
- Tests with `pytest` + `pytest-asyncio`

## Architecture Rules

- **Zero server modification** — never assume server-side changes. Only standard UO packets.
- **Packet codec in `anima/client/`** — all packet encoding/decoding lives here. Other layers never deal with raw bytes.
- **Perception layer is the single source of truth** — packet handlers update `WorldState`, brain reads `WorldState`. Brain never parses packets directly.
- **3-tier decision** — Tier 1 (behavior tree, instant, free) → Tier 2 (small LLM, ~100ms) → Tier 3 (large LLM, ~1-3s). Escalate only when needed.
- **LLM interface is abstract** — `LLMClient` supports both Ollama (local) and OpenAI-compatible APIs. Default is Ollama.

## Packet Protocol Notes

- servuo-rs has **no encryption** — send plaintext TCP
- **Huffman compression** is required for game-phase server→client packets only
- Two-connection login flow: Connection 1 (account) → Connection 2 (game)
- All network values are **Big-Endian**
- Packet format: fixed = `[ID][payload]`, variable = `[ID][length BE u16][payload]`
- Reference packet table: `servuo-rs/crates/servuo-protocol/src/lib.rs` (PACKET_LENGTHS)
- Reference test client: `servuo-rs/tests/integration/test_client.rs`

## Movement Protocol

- Walk packet (0x02): `[dir|run_flag] [seq] [fastwalk_key:u32]` — 7 bytes
- Sequence: 1-255, wraps to 1 (never 0)
- Max 5 pending steps
- Server responds: ConfirmWalk (0x22) or DenyWalk (0x21)
- Throttle: 400ms walk, 200ms run, 100ms mounted run

## Development Workflow

- Package manager: `uv`
- Run: `uv run python -m anima`
- Test: `uv run pytest`
- Lint: `uv run ruff check`
- Format: `uv run ruff format`

## File Organization

When adding new packet handlers:
1. Add packet length to `anima/client/packets.py` (PACKET_LENGTHS)
2. Add builder function to `anima/client/packets.py` (for outgoing)
3. Add handler method to `anima/client/parser.py` (for incoming)
4. Update `WorldState` in `anima/perception/` from the handler

When adding new AI behaviors:
1. Add action implementation to `anima/action/`
2. Add behavior tree node to `anima/brain/behavior_tree.py`
3. Wire into persona schedule if it's a routine behavior

When adding new skills (RL-driven actions):
1. Create skill class in `anima/skills/<category>/<name>.py` extending `Skill` ABC
2. Set `name`, `category`, `description`, preconditions (`required_items`, `required_nearby`, etc.)
3. Implement `can_execute(ctx)` and `execute(ctx) -> SkillResult`
4. Register in `main.py` via `skill_registry.register(MySkill())`
5. Q-table handles selection automatically — no BT changes needed
6. See `docs/reinforcement-learning.md` for reward design guidelines

## AI & RL Architecture

- **Behavior Tree** runs every 200ms: Survival → Social → Forum → SkillExec → Think
- **SkillExec** uses Q-learning + UCB1 to select from available skills
- **Think** uses LLM for strategic decisions (where to go, what to focus on)
- RL stats (Q-values, location values) are injected into LLM prompts via `memory/retrieval.py`
- Skills return `SkillResult` with reward signals — Q-table updates automatically
- State is encoded as `"location_type|player_presence|enemy_presence|hp_level|inventory"` string
- Location-activity value map tracks reward per 32×32 tile region
