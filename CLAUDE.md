# CLAUDE.md — Agent Work Rules

## Project Overview

Anima is a Python-based AI player system for Ultima Online. It connects to a UO server as an external client using the standard UO packet protocol.

## Key References

- `docs/FOUNDRY.md` — **Foundry: the self-developing agent system** (evolution loop,
  trusted kernel, MAP-Elites grid, locked fitness/descriptor). `foundry/README.md`
  has the runbook. `foundry/kernel/` is HUMAN-OWNED — agents must never edit it.
- `DESIGN.md` — Full system design (architecture, roadmap, tech stack)
- `docs/classicuo-analysis.md` — ClassicUO protocol analysis (packet handlers, entity model, all subsystems)
- `docs/implementation-plan.md` — Concrete implementation plan (module mapping, code sketches)
- `docs/skill-system.md` — Skill system design (skill catalog, packet requirements, file structure)
- `docs/reinforcement-learning.md` — **RL 학습 방법론** (Q-learning, UCB1, state encoding, reward signals, LLM 연동)
- ClassicUO source: `~/dev/uo/classicuo/` (C# reference client)

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
- **Planner 기반 의사결정** — v2 Planner가 우선순위 규칙으로 procedure 선택. v1 행동트리는 `--legacy` 옵션으로 사용 가능.
- **LLM interface is abstract** — `LLMClient` supports both Ollama (local) and OpenAI-compatible APIs. Default is Ollama.

## Packet Protocol Notes

- **No encryption** — send plaintext TCP
- **Huffman compression** is required for game-phase server→client packets only
- Two-connection login flow: Connection 1 (account) → Connection 2 (game)
- All network values are **Big-Endian**
- Packet format: fixed = `[ID][payload]`, variable = `[ID][length BE u16][payload]`

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

### Foundry (evolution loop)

- Boot shard: `cd ~/dev/uo/servuo && MONO_GAC_PREFIX=/opt/homebrew mono ServUO.exe -noconsole`
  (listens on 127.0.0.1:**2594**)
- One live eval: `uv run python -m foundry.kernel.eval --user probe1 --window 600`
- Evolution run: `uv run python -m foundry.orchestrator --cycles 4 --parallel 2 --window 600 --backend claude`
- Grid status: `uv run python -m foundry.status` · Halt: `touch foundry/STOP`

## Wiki discrepancy reports

When gameplay contradicts the companion wiki (`../uowiki`) — wrong numbers, behavior
that doesn't match a page, or a missing page worth proposing — file a report:

```
python3 tools/wiki_report.py --agent bjorn --page src/content/docs/skills/mining.md \
  --claim "..." --observed "... + log excerpt" --expected "..." --evidence "agent/timestamp/log path"
```

Reports land in `../uowiki/reports/open/` where a librarian routine triages them daily.
Use `--force` for missing pages, `--commit` to commit in the wiki repo (never push).
Format and triage rules: `../uowiki/CLAUDE.md` ("Discrepancy reports").

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
- **SkillExec** currently uses random selection (Q-learning disabled, planned for Phase 3)
- **Think** uses LLM for strategic decisions (where to go, what to focus on)
- RL stats (Q-values, location values) are injected into LLM prompts via `memory/retrieval.py`
- Skills return `SkillResult` with reward signals — Q-table updates automatically
- State is encoded as `"location_type|player_presence|enemy_presence|hp_level|inventory"` string
- Location-activity value map tracks reward per 32×32 tile region
