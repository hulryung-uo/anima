# Anima — UO AI Player System

> *Anima (Latin: soul)* — An AI player system that breathes souls into the Ultima Online world

## 1. Project Vision

### What We're Building
An AI player system that **connects to the UO server (`servuo-rs`) as a real client** and acts autonomously.
From the server's perspective, humans and AI are indistinguishable. The AI is not an internal server module — it is an **independent external client**.

### Why We're Building It
- **Living World**: The world runs even with zero human players. Log in to find a world that already has history and economy.
- **Dynamic Content**: Not static NPC dialogue, but inhabitants that perceive and react to situations.
- **Economic Homeostasis**: AI producers/consumers maintain the market, preventing hyper-inflation or deflation.
- **Technical Experiment**: Exploring the intersection of LLM + game simulation.

### Core Principles
1. **Zero Server Modification** — Do not modify servuo-rs. Communicate only via the standard UO packet protocol.
2. **External Client** — Connect the same way ClassicUO does. The server does not treat AI specially.
3. **Tiered Intelligence** — Don't delegate all decisions to the LLM. 90% rule-based, 8% small model, 2% large model.
4. **Observability** — AI thought processes, decisions, and memories can be monitored in real time.
5. **Incremental Build** — Start with walking. Add behaviors one step at a time.
6. **Local LLM First** — Use Ollama-based local models to eliminate API costs. Cloud API is optional.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Anima Orchestrator                     │
│                                                          │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │
│  │ Agent #1 │ │ Agent #2 │ │ Agent #3 │ │ Agent #N │   │
│  │ "Cheolsu"│ │ "Younghee"│ │"Blacksmth"│ │  ...     │   │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘   │
│       │             │            │             │         │
│  ┌────▼─────────────▼────────────▼─────────────▼─────┐  │
│  │              Shared Infrastructure                 │  │
│  │  ┌─────────┐ ┌──────────┐ ┌────────┐ ┌─────────┐ │  │
│  │  │ LLM Pool│ │ Memory DB│ │ Logger │ │ Metrics │ │  │
│  │  │ (Ollama)│ │ (SQLite) │ │        │ │         │ │  │
│  │  └─────────┘ └──────────┘ └────────┘ └─────────┘ │  │
│  └────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
           │ │ │ ... │  (independent TCP connections)
           ▼ ▼ ▼     ▼
┌─────────────────────────────────────────────────────────┐
│                   servuo-rs Server                        │
│              (Standard UO Packet Protocol)                │
└─────────────────────────────────────────────────────────┘
```

### 2.1 Agent Internal Structure

Each AI Agent consists of 4 layers:

```
┌─────────────────────────────────────────┐
│           Agent "Cheolsu"                │
│                                         │
│  ┌─────────────────────────────────┐    │
│  │  (1) Connection Layer           │    │
│  │  - TCP connection, packet codec │    │
│  │  - Login/character select auto  │    │
│  │  - Packet send/receive queue    │    │
│  └──────────┬──────────────────────┘    │
│             │ Parsed events              │
│  ┌──────────▼──────────────────────┐    │
│  │  (2) Perception Layer           │    │
│  │  - World State (nearby mobiles) │    │
│  │  - Self State (HP, stats, inv)  │    │
│  │  - Social State (chat, relations)│   │
│  │  - Spatial State (position, LOS)│    │
│  └──────────┬──────────────────────┘    │
│             │ Structured world model     │
│  ┌──────────▼──────────────────────┐    │
│  │  (3) Brain Layer                │    │
│  │  - Behavior Tree (routines)     │    │
│  │  - Goal System (goal mgmt)      │    │
│  │  - LLM Interface (Ollama)       │    │
│  │  - Personality (traits)         │    │
│  │  - Memory (long/short term)     │    │
│  └──────────┬──────────────────────┘    │
│             │ Decided actions            │
│  ┌──────────▼──────────────────────┐    │
│  │  (4) Action Layer               │    │
│  │  - Movement (pathfinding)       │    │
│  │  - Combat (target, skill, flee) │    │
│  │  - Speech (speech packets)      │    │
│  │  - Trade (secure trade, vendor) │    │
│  │  - Skill usage                  │    │
│  └─────────────────────────────────┘    │
└─────────────────────────────────────────┘
```

### 2.2 Project Structure

```
anima/
├── pyproject.toml              # Project config (uv)
├── DESIGN.md                   # This document
├── CLAUDE.md                   # Agent work rules
│
├── anima/                      # Main package
│   ├── __init__.py
│   ├── main.py                 # Entry point
│   │
│   ├── client/                 # UO protocol client
│   │   ├── __init__.py
│   │   ├── codec.py            # Packet codec (encryption, compression)
│   │   ├── connection.py       # TCP connection management, login flow
│   │   ├── packets.py          # Client→Server packet encoding
│   │   └── parser.py           # Server→Client packet decoding
│   │
│   ├── perception/             # World perception & state tracking
│   │   ├── __init__.py
│   │   ├── world_state.py      # Nearby entity tracking (mobiles, items, terrain)
│   │   ├── self_state.py       # Self state (HP, stats, skills, inventory)
│   │   ├── social_state.py     # Chat log, relationship tracking
│   │   └── event_stream.py     # Packet → meaningful game event conversion
│   │
│   ├── brain/                  # Decision engine
│   │   ├── __init__.py
│   │   ├── behavior_tree.py    # Rule-based behavior tree
│   │   ├── goal_system.py      # Goal management (long/short term)
│   │   ├── personality.py      # Personality parameters & tendencies
│   │   ├── llm.py              # LLM interface (Ollama + OpenAI compatible)
│   │   ├── prompt.py           # Situation summary → LLM prompt generation
│   │   └── decision.py         # 3-tier decision router
│   │
│   ├── memory/                 # Memory system
│   │   ├── __init__.py
│   │   ├── short_term.py       # Short-term memory (recent event buffer)
│   │   ├── long_term.py        # Long-term memory (SQLite)
│   │   ├── episodic.py         # Episodic memory (events/experiences)
│   │   ├── semantic.py         # Semantic memory (knowledge/facts)
│   │   └── retrieval.py        # Relevant memory retrieval (embedding-based)
│   │
│   ├── action/                 # Action execution
│   │   ├── __init__.py
│   │   ├── movement.py         # Movement & pathfinding
│   │   ├── combat.py           # Combat actions (attack, defend, flee)
│   │   ├── speech.py           # Speech (dialogue, shouts)
│   │   ├── trade.py            # Trading (NPC vendor, player trade)
│   │   ├── skills.py           # Skill usage (gathering, crafting)
│   │   └── interaction.py      # Object interaction (doors, containers)
│   │
│   ├── persona/                # Persona definition & management
│   │   ├── __init__.py
│   │   ├── template.py         # YAML-based persona definition
│   │   ├── schedule.py         # Daily schedule (sleep, activity periods)
│   │   └── relationships.py    # Inter-NPC relationship graph
│   │
│   └── orchestrator/           # Multi-agent management
│       ├── __init__.py
│       ├── manager.py          # Agent creation/termination/monitoring
│       ├── economy.py          # Economic balance monitoring
│       ├── dashboard.py        # Web dashboard (observation/debugging)
│       └── config.py           # System-wide configuration
│
├── personas/                   # Persona definition files
│   ├── blacksmith.yaml         # Blacksmith
│   ├── merchant.yaml           # Merchant
│   ├── adventurer.yaml         # Adventurer
│   ├── guard.yaml              # Guard
│   └── thief.yaml              # Thief
│
├── data/
│   ├── maps/                   # Map data for pathfinding
│   └── knowledge/              # World base knowledge (cities, NPCs, items)
│
├── tests/                      # Tests
│   ├── test_client.py
│   ├── test_perception.py
│   └── ...
│
└── tools/
    └── dashboard/              # Monitoring web UI
```

---

## 3. Core Systems Detail

### 3.1 Connection Layer — UO Protocol Client

Implemented in Python, referencing the `servuo-rs` E2E test client (`tests/integration/test_client.rs`).
Implements the same protocol flow as a real ClassicUO client.

```
Login Flow:
1. TCP connect (game port)
2. 0xEF LoginSeed → 0x80 AccountLogin
3. 0xA8 ServerList received → 0xA0 ServerSelect
4. 0x8C Redirect received → new TCP connection
5. 0x91 GameLogin → 0xA9 CharacterList
6. 0x5D CharacterSelect → 0x1B LoginConfirm + 0x55 LoginComplete

Game Loop:
- Receive server packets → convert to events → update Perception
- Brain tick (100ms interval) → decide Action
- Action → encode packets → send to server
```

**Packet Implementation Strategy**:
- Binary packet encoding/decoding via Python `struct` module
- `asyncio`-based TCP connection management
- Independent Python implementation, referencing servuo-rs packet definitions

### 3.2 Perception Layer — World Perception

Maintains the world as the AI "sees" it in structured data.

```python
@dataclass
class WorldView:
    """World state as perceived by the AI Agent"""

    # Spatial awareness
    my_position: Point3D
    nearby_mobiles: dict[int, MobileInfo]    # serial → mobiles in view
    nearby_items: dict[int, ItemInfo]         # serial → items in view
    known_locations: dict[str, Point3D]       # known places

    # Self awareness
    my_stats: Stats                           # HP, Mana, Stam
    my_skills: dict[int, float]               # skill values
    my_inventory: list[ItemInfo]              # belongings
    my_equipment: dict[str, ItemInfo]          # equipped items

    # Social awareness
    recent_speech: deque[SpeechEvent]         # recent dialogue
    known_players: dict[int, PlayerRelation]   # known players
    threat_level: ThreatLevel                  # current threat level
```

State is updated as server packets arrive:

| Server Packet | WorldView Update |
|---|---|
| 0x78 MobileIncoming | Add/update `nearby_mobiles` |
| 0x1D DeleteEntity | Remove from `nearby_mobiles` / `nearby_items` |
| 0x1A WorldItem | Add to `nearby_items` |
| 0xAE UnicodeSpeech | Append to `recent_speech`, record speaker |
| 0x20 MobileUpdate | Update position/state |
| 0x2E EquipItem | Update equipment info |
| 0xA1 StatUpdate | Update HP/Mana/Stam |
| 0x3A SkillUpdate | Update skill values |

### 3.3 Brain Layer — 3-Tier Decision Making

Delegating all decisions to LLM causes latency explosion. We split into 3 tiers.

#### Tier 1: Behavior Tree (Rule-based, instant, zero cost)

~90% of all decisions. Repetitive, pattern-based behaviors.

```
Root
├── [Priority] Survival
│   ├── HP < 30% → use potion or flee
│   ├── Poisoned → use cure
│   └── Enemy approaching → switch to combat mode
│
├── [Priority] Daily Routine
│   ├── Check schedule → current time period activity
│   ├── Blacksmith: mine ore → smelt → craft → sell
│   ├── Merchant: check stock → buy → sell → travel
│   └── Adventurer: explore dungeon → fight → return
│
├── [Sequence] Social Response
│   ├── Someone spoke to me? → escalate to Tier 2/3
│   ├── Greeting pattern match → templated greeting response (Tier 1)
│   └── Trade request → execute trade routine
│
└── [Fallback] Free Behavior
    ├── Has goal → pursue goal
    └── No goal → wander / observe
```

#### Tier 2: Small Local LLM (~100ms)

~8% of all decisions. When simple contextual judgment is needed.

Model: `gemma3:4b` or `llama3.2:3b` (Ollama)

Trigger conditions:
- Simple dialogue responses ("hello", "where is the bank?", "how much is this?")
- Simple reactions to unexpected situations
- Item value judgment (pick up vs ignore)

```
Prompt example:
"You are 'Cheolmin', a blacksmith in Britain. Personality: gruff but kind.
 Situation: Adventurer 'Player123' said: 'Make me a sword'
 Current stock: 45 iron ingots, crafting possible.
 Choose an action: (sell_item / refuse / ask_price / negotiate)"
```

#### Tier 3: Large Local LLM (~1-3s)

~2% of all decisions. Complex, important decisions.

Model: `llama3.1:8b` or `gemma3:12b` (Ollama). Larger models possible with sufficient GPU.

Trigger conditions:
- Deep conversations (free-form dialogue with human players)
- Strategic decisions (guild membership, trade alliances, territory moves)
- Conflict resolution (robbed by thieves, dispute mediation)
- New goal setting (long-term planning)

```
Prompt example:
"## Persona
Name: Cheolmin, Profession: Blacksmith, Personality: [detailed description]
Current goal: Become the best blacksmith in Britain

## Memories
- 3 days ago: Crafted a fine sword for Player456, who became a regular
- Yesterday: Ore was stolen by bandits at the mine
- Today: Player789 proposed guild membership

## Current Situation
[detailed situation context]

## Choices
How will you act in this situation? Respond with your reasoning."
```

#### Escalation Rules

```
Can be handled at Tier 1 → execute Tier 1 (instant)
     │
     ├── Dialogue pattern match fails → Tier 2
     ├── Unexpected event → Tier 2
     ├── Threat situation + complex judgment → Tier 2
     │
     └── Tier 2 returns "uncertain" → Tier 3
         3+ turns of conversation with human player → Tier 3
         Long-term goal change needed → Tier 3
         Significant economic decision (large trade) → Tier 3
```

### 3.4 Memory System

Remembers and forgets like a human.

#### Short-term Memory
- Ring buffer, last 50 events
- "What just happened?"
- In-memory only, no persistence

#### Episodic Memory
- Personal experiences/events
- "3 days ago I crafted a sword for Player456"
- Includes emotion tags (positive/negative/neutral)
- Decays or compresses over time based on importance

#### Semantic Memory
- Learned facts/knowledge
- "Britain blacksmith shop is at (1450, 1620)"
- "10 iron ingots can make a longsword"
- Rarely forgotten

#### Relational Memory
- Relationships with other entities
- "Player456: regular customer, high trust, last trade 3 days ago"
- "Bandit gang: hostile, be careful near the mine"

#### Storage

```
SQLite (anima.db)
├── episodic_memories
│   ├── agent_id
│   ├── timestamp
│   ├── summary          # text summary
│   ├── emotion          # emotion tag
│   ├── importance       # importance (0.0-1.0)
│   ├── embedding        # vector (for retrieval)
│   └── decay_at         # scheduled decay time
│
├── semantic_memories
│   ├── agent_id
│   ├── fact             # fact text
│   ├── confidence       # confidence level
│   ├── source           # source (experience/hearsay)
│   └── embedding
│
└── relationships
    ├── agent_id
    ├── target_serial
    ├── target_name
    ├── disposition       # -1.0 (hostile) ~ 1.0 (friendly)
    ├── trust             # trust level
    ├── last_interaction
    └── notes             # free-text notes
```

#### Memory Retrieval

Process for selecting memories to include in LLM prompts:

```
Current situation → generate situation summary text
                │
                ├── 1) Recency: top 5 recent episodes
                ├── 2) Relevance: top 5 by cosine similarity with situation embedding
                ├── 3) Importance: top 3 by importance score
                └── 4) Relationship: relationship memory for current interaction target
                │
                ▼
        Deduplicate → trim to token budget → insert into prompt
```

Embedding generation: Processed locally using Ollama embedding model (`nomic-embed-text`, etc.).

### 3.5 Persona System

Personas are defined in YAML files.

```yaml
# personas/blacksmith.yaml
name: "Cheolmin"
title: "Britain Blacksmith"

# Personality (Big Five based)
personality:
  openness: 0.3           # conservative, traditional
  conscientiousness: 0.9   # diligent and meticulous
  extraversion: 0.4        # slightly introverted
  agreeableness: 0.7       # generally kind
  neuroticism: 0.2         # emotionally stable

# Speech style
speech_style: "Gruff but warm tone. Uses formal speech. Frequently uses blacksmithing jargon."
speech_examples:
  - "Need a sword? Just got some fine iron in."
  - "A repair like this... 50 gold should cover it."
  - "No funny business. I'm a busy man."

# Daily schedule (server time)
schedule:
  - { hour: 6,  activity: wake_up,    location: "Britain_BlacksmithHome" }
  - { hour: 7,  activity: work,       location: "Britain_BlacksmithShop" }
  - { hour: 12, activity: break,      location: "Britain_Tavern" }
  - { hour: 13, activity: work,       location: "Britain_BlacksmithShop" }
  - { hour: 18, activity: socialize,  location: "Britain_Tavern" }
  - { hour: 21, activity: sleep,      location: "Britain_BlacksmithHome" }

# Professional behavior
profession:
  type: blacksmith
  primary_skill: blacksmithy
  secondary_skills: [mining, arms_lore]
  products: [longsword, plate_armor, shield]
  buy_materials: [iron_ingot, coal]
  preferred_mine: "Britain_NorthMine"

# Goals
goals:
  - { goal: "Become recognized as the best blacksmith in Britain", priority: high, type: long_term }
  - { goal: "Maintain ore stock above 100", priority: medium, type: recurring }
  - { goal: "Find an apprentice", priority: low, type: long_term }

# Initial relationships
initial_relationships:
  - { name: "Miner Kim", disposition: 0.6, note: "ore supplier" }
  - { name: "Merchant Park", disposition: 0.5, note: "consignment sales" }

# Economic behavior
economy:
  base_gold: 500
  pricing_strategy: cost_plus_20_percent
  haggle_tolerance: 0.1  # willing to discount up to 10%
```

### 3.6 Pathfinding

Pathfinding is essential for AI to navigate the world.

#### Map Data
- Load tile data from servuo-rs `data/` or UO client files
- Extract passable/impassable tile information
- Consider Z level (height)

#### Algorithms
- **Local movement** (within line of sight): A* (short distance, instant calculation)
- **Long-distance movement** (between cities): pre-computed waypoint graph + A*
- **Doors/teleporters**: included as special nodes in the graph

```
Waypoint Graph:
  Britain_Bank ←→ Britain_Blacksmith ←→ Britain_Gate
       ↕                                      ↕
  Britain_Tavern                        Moongate_Felucca
                                              ↕
                                        Vesper_Gate
```

### 3.7 Economy System

AI players actually gather resources, craft items, and trade.

#### Economic Cycle

```
  Miner AI                Blacksmith AI            Adventurer AI
  ┌──────┐    iron ore    ┌──────┐   longsword   ┌──────┐
  │ Mine  │──────────────→│ Craft │─────────────→│ Use   │
  │      │    gold ←──────│      │   gold ←──────│      │
  └──────┘                └──────┘               └──────┘
     ↑                                               │
     │              monster loot (gold, ore)          │
     └───────────────────────────────────────────────┘
```

#### Pricing
- Base price: cost + margin defined in persona
- Supply/demand: discount when stock is high, markup when low
- Haggling: discount range determined by personality.agreeableness
- Natural haggling dialogue with human players via Tier 2/3 LLM

#### Orchestrator Economic Monitoring
- Monitor total gold across all AI agents
- Adjust pricing parameters when inflation/deflation detected
- Steer AI behavior when specific resources are depleted (increase gathering priority)

---

## 4. Orchestrator — Multi-Agent Management

### 4.1 Agent Lifecycle

```
     Definition (persona YAML)
          │
          ▼
     Create → connect to server → character login
          │
          ▼
     ┌── Active Loop ──┐
     │  Perceive        │
     │  Think           │  ← 100ms tick
     │  Act             │
     │  Remember        │
     └─────────────────┘
          │
     Logout per schedule (sleep time)
          │
     Re-login (wake time)
```

### 4.2 Observation/Debugging Dashboard

Real-time monitoring in a web browser.

```
┌─────────────────────────────────────────────────┐
│  Anima Dashboard                          [Live] │
├──────────────┬──────────────────────────────────┤
│ Agent List   │  Agent: Cheolmin (Blacksmith)     │
│              │                                   │
│ ● Cheolmin   │  Status: Working                  │
│ ● Younghee   │  Location: Britain Blacksmith Shop │
│ ○ Thief Kim  │  HP: 100/100  Gold: 342           │
│ ● Miner Park │                                   │
│              │  [Current Goal]                    │
│              │  "Craft 3 longswords"              │
│              │                                   │
│              │  [Thought Log]                     │
│              │  14:23 Tier1: Check ore stock → OK  │
│              │  14:23 Tier1: Begin crafting        │
│              │  14:25 Tier2: Player spoke          │
│              │    → "Need a sword?"                │
│              │  14:25 Tier3: Deep conversation     │
│              │    → [View prompt/response]         │
│              │                                   │
│              │  [Memory Highlights]               │
│              │  - Player456 is a regular (trust 0.8)│
│              │  - Ore prices trending up           │
│              │                                   │
│              │  [Economy]                         │
│              │  Today: 3 sales, income 150g       │
│              │  Stock: iron ingot 45, steel 12    │
├──────────────┴──────────────────────────────────┤
│ LLM Calls: 23 today | Avg: 180ms | Model: 8B    │
└─────────────────────────────────────────────────┘
```

---

## 5. Local LLM Configuration

### 5.1 Ollama-based Inference Server

Run LLM locally for zero API cost operation.

```
┌──────────────┐     HTTP (localhost:11434)     ┌──────────────┐
│    Anima     │ ──────────────────────────────→ │    Ollama    │
│  (Python)    │ ←────────────────────────────── │   Server     │
│              │   OpenAI-compatible API          │              │
│              │   /v1/chat/completions           │  ┌────────┐ │
│              │   /api/embeddings                │  │ Model  │ │
│              │                                  │  │ Files  │ │
└──────────────┘                                  │  └────────┘ │
                                                  └──────────────┘
```

### 5.2 Model Configuration

| Tier | Purpose | Recommended Model | VRAM | Response Time |
|---|---|---|---|---|
| Tier 2 | Simple judgment/dialogue | `gemma3:4b` or `llama3.2:3b` | ~3GB | ~100ms |
| Tier 3 | Deep dialogue/strategy | `llama3.1:8b` or `gemma3:12b` | ~5-8GB | ~1-3s |
| Embedding | Memory retrieval | `nomic-embed-text` | ~300MB | ~10ms |

### 5.3 LLM Interface Design

Uses Ollama's OpenAI-compatible API, abstracted for future cloud API migration.

```python
class LLMClient:
    """LLM inference client (Ollama / OpenAI compatible)"""

    async def chat(self, messages: list[dict], model: str = None) -> str:
        """Chat completion"""
        ...

    async def embed(self, text: str) -> list[float]:
        """Text → embedding vector"""
        ...
```

Backend is swappable via config file:

```yaml
# config.yaml
llm:
  backend: ollama                    # ollama | openai
  base_url: http://localhost:11434
  tier2_model: gemma3:4b
  tier3_model: llama3.1:8b
  embedding_model: nomic-embed-text
  timeout: 10                        # seconds
  # To switch to cloud:
  # backend: openai
  # base_url: https://api.anthropic.com
  # tier2_model: claude-haiku-4-5-20251001
  # tier3_model: claude-sonnet-4-6
```

---

## 6. Development Roadmap

### Phase 0: Foundation (3-5 days)
**Goal**: Project setup, server connection, basic movement

- [ ] Create Python project (pyproject.toml, uv)
- [ ] `anima/client/`: connect to servuo-rs, login, character select
  - Reference `servuo-rs`'s `test_client.rs`, implement in Python
  - Packet codec via `asyncio` + `struct` module
- [ ] Packet receive → console log output (verify world is visible)
- [ ] Basic movement: walk in random directions
- [ ] Single agent execution verified

**Done when**: AI connects to the server and walks around the streets of Britain.

### Phase 1: Perception (1 week)
**Goal**: The AI "sees" the world

- [ ] `anima/perception/`: WorldView implementation
- [ ] Parse major server packets (Mobile, Item, Speech, Stat, etc.)
- [ ] Self state tracking (HP, position, inventory)
- [ ] Nearby entity tracking (mobiles/items in view range)
- [ ] Event stream: packet → meaningful game event conversion

**Done when**: AI perceives its surroundings as structured data.

### Phase 2: Basic Brain (1 week)
**Goal**: Rule-based behavior

- [ ] `anima/brain/`: Behavior tree framework
- [ ] `anima/action/`: Basic actions (move, pick up items, attack)
- [ ] Pathfinding (local A*)
- [ ] Survival behavior: detect danger → flee, use potions
- [ ] Simple combat: find monster → attack → loot

**Done when**: AI hunts monsters and loots items outside Britain.

### Phase 3: LLM Integration (1 week)
**Goal**: The AI "thinks"

- [ ] Ollama integration (`anima/brain/llm.py`)
- [ ] `anima/brain/prompt.py`: situation summary prompt generation
- [ ] `anima/brain/decision.py`: 3-tier escalation router
- [ ] Dialogue: respond via LLM when a human speaks
- [ ] Tier 1/2/3 routing validation

**Done when**: AI responds with contextually appropriate natural dialogue when spoken to.

### Phase 4: Memory (1 week)
**Goal**: The AI "remembers"

- [ ] `anima/memory/`: SQLite-based memory store
- [ ] Short-term/episodic/semantic/relational memory implementation
- [ ] Memory retrieval (Ollama embedding + cosine similarity)
- [ ] Memory decay/compression (over time)
- [ ] Auto-inject relevant memories into LLM prompts

**Done when**: AI remembers a player it met yesterday and mentions them.

### Phase 5: Persona & Schedule (3-5 days)
**Goal**: Give AI personality and daily routines

- [ ] `anima/persona/`: YAML-based persona loader
- [ ] Schedule system: activity transitions by time of day
- [ ] Personality reflected in: speech style, decision tendencies, dialogue style
- [ ] 3 base personas: blacksmith, merchant, adventurer

**Done when**: Blacksmith AI wakes up in the morning, goes to the workshop, and heads to the tavern in the evening.

### Phase 6: Economy (1-2 weeks)
**Goal**: AI participates in the economy

- [ ] Resource gathering actions (Mining, Lumberjacking)
- [ ] Crafting actions (Blacksmithy, Tailoring)
- [ ] Trading actions (NPC vendor buy/sell, player trade)
- [ ] Pricing logic (supply/demand reflected)
- [ ] Orchestrator economic monitoring

**Done when**: Miner → Blacksmith → Merchant economic cycle runs autonomously.

### Phase 7: Multi-Agent & Social (1-2 weeks)
**Goal**: AI-to-AI interaction

- [ ] Orchestrator: concurrent multi-agent management
- [ ] AI-to-AI dialogue (Tier 1 pattern matching preferred, LLM when needed)
- [ ] Relationship system: affinity, trust changes
- [ ] Group behavior: group hunting, cooperative defense
- [ ] 10 simultaneous agents stability test

**Done when**: 10 AI agents perform their roles and interact in Britain.

### Phase 8: Dashboard & Observability (3-5 days)
**Goal**: Observe and debug

- [ ] Web dashboard (FastAPI + htmx or separate frontend)
- [ ] Real-time agent status display
- [ ] Thought log (Tier 1/2/3 decision history)
- [ ] LLM call log (prompts, responses, speed)
- [ ] Economic indicator graphs

**Done when**: All AI states and thought processes are viewable in real time via browser.

### Phase 9: Polish & Scale (2 weeks)
**Goal**: Stabilize and scale

- [ ] 50 simultaneous agents stress test
- [ ] LLM inference optimization (batching, prompt caching, KV cache)
- [ ] Memory system optimization (retrieval speed, storage capacity)
- [ ] Long-term operational stability (24h+ uninterrupted)
- [ ] Additional personas (guard, thief, priest, fisherman, etc.)

**Done when**: 50 AI agents operate a living world stably for 24 hours.

---

## 7. Tech Stack

| Area | Technology | Rationale |
|---|---|---|
| Language | Python 3.12+ | Fast iteration, strongest LLM ecosystem |
| Package Manager | uv | Fast, modern Python package manager |
| Async Runtime | asyncio | Network I/O + timers, standard library |
| LLM Inference | Ollama (local) | Zero cost, OpenAI-compatible API, easy setup |
| LLM Client | httpx / openai SDK | Uses Ollama's OpenAI-compatible endpoint |
| Memory Storage | SQLite (aiosqlite) | Lightweight, embedded, async support |
| Vector Search | numpy + cosine similarity | Lightweight implementation, sufficient at small scale |
| Embedding | Ollama (nomic-embed-text) | Local embedding, zero cost |
| Serialization | PyYAML / Pydantic | Persona definitions, config, type validation |
| Pathfinding | Custom A* (heapq) | UO map specific (Z levels, movement rules) |
| Logging | structlog | Structured logging |
| Dashboard | FastAPI + htmx | Lightweight web UI, WebSocket real-time |
| Testing | pytest + pytest-asyncio | Async test support |
| Packet Protocol | struct module (custom) | Reference servuo-rs, independent Python impl |

---

## 8. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Local LLM quality insufficient | Unnatural dialogue | Prompt optimization, model upgrade, cloud fallback if needed |
| Local LLM concurrent request bottleneck | Response delays | Ollama queue management, Tier 1 fallback, request prioritization |
| GPU memory shortage | Model loading failure | Use smaller models, quantization (Q4), same model for Tier 2/3 |
| LLM breaks character | Immersion broken | Strengthen personality constraint prompts, output filtering |
| Economic imbalance | Inflation/deflation | Orchestrator monitoring + auto-intervention parameters |
| Unbounded memory growth | Storage/retrieval perf degradation | Memory decay policy, compression, importance-based cleanup |
| Server protocol changes | Client compatibility broken | Reference servuo-rs source, packet version management |
| AI-to-AI chat inference explosion | GPU overload | AI-to-AI dialogue prefers Tier 1, cooldown limits |
| Python GIL bottleneck (50+ agents) | Performance degradation | Mostly asyncio I/O-bound, limited impact. Multiprocess if severe |

---

## 9. Success Criteria

### Phase 0-2 (Technical Validation)
- [ ] Server connection success rate 99%+
- [ ] Packet parsing coverage 80%+
- [ ] Pathfinding success rate 90%+

### Phase 3-5 (AI Quality)
- [ ] Difficult to distinguish from human in dialogue (blind test pass rate 60%+)
- [ ] Schedule adherence rate 95%+
- [ ] Appropriate tier escalation (unnecessary LLM calls < 5%)

### Phase 6-9 (System Stability)
- [ ] 24-hour uninterrupted operation
- [ ] 50 simultaneous agents add < 10% server CPU overhead
- [ ] Human players feel "this world is alive"
