# Anima Architecture v2 — Design Document

## 1. Core Insight

UO gameplay is **procedural**, not **exploratory**.

A miner follows known procedures: mine ore, smelt, bank, craft, sell. The procedures themselves are fixed packet sequences with wait conditions. The AI doesn't need to "discover" how to mine through reinforcement learning — it needs to:

1. **Execute known procedures correctly** (handle timing, errors, edge cases)
2. **Decide when to switch between procedures** (strategic planning)
3. **Adapt to the world state** (ore depleted, tool broken, overweight, attacked)

This means Q-learning is the wrong tool. The right model is:
**Procedures (known) + Planner (reactive/LLM) + World Model (perception)**

---

## 2. Layer Architecture

```
+------------------------------------------------------------------+
|  Layer 6: Character Definition (Markdown)                        |
|  "hully is a miner and blacksmith"                               |
|  -> skills, goals, personality, known locations                  |
+------------------------------------------------------------------+
|  Layer 5: Planner (Rule-based + LLM escalation)                  |
|  Evaluates world state -> picks next procedure                   |
|  "overweight? -> smelt. no tools? -> tinker. nothing to do? ->   |
|   mine. inventory full of ingots? -> craft. crafted items? ->    |
|   sell."                                                         |
+------------------------------------------------------------------+
|  Layer 4: Procedures (Game Workflows)                            |
|  mine_ore, smelt_ore, bank_deposit, craft_item, sell_to_vendor,  |
|  buy_from_vendor, make_tools, train_skill                        |
|  Each is a state machine of primitives with error recovery       |
+------------------------------------------------------------------+
|  Layer 3: Action Primitives (Packet Sequences)                   |
|  use_object, target_tile, target_object, move_to, say,           |
|  gump_respond, buy_items, sell_items, drag_drop                  |
|  Each handles send + wait + timeout + retry                      |
+------------------------------------------------------------------+
|  Layer 2: World Model (Perception)                               |
|  Items, Mobiles, Map, Skills, Stats, Journal, Containers         |
|  Updated from incoming packets, single source of truth           |
+------------------------------------------------------------------+
|  Layer 1: Protocol (Network)                                     |
|  TCP, Huffman, packet encode/decode, connection management       |
+------------------------------------------------------------------+
```

---

## 3. Layer Details

### Layer 1: Protocol

Already well-implemented. Handles:
- TCP connection with auto-reconnect
- Huffman decompression (server->client)
- Packet encode/decode (Big-Endian)
- Two-phase login (account -> game)

No major changes needed.

### Layer 2: World Model

The world model tracks everything the avatar knows. Updated exclusively from incoming packets. Other layers read but never write.

```
WorldModel
  +-- self: PlayerState
  |     serial, name, x, y, z, direction
  |     hp, mana, stam, str, dex, int
  |     gold, weight, weight_max
  |     skills: dict[SkillId, SkillInfo]     # value, cap, lock
  |     equipment: dict[Layer, ItemSerial]
  |
  +-- items: dict[Serial, Item]
  |     serial, graphic, hue, amount
  |     x, y, z (world) or slot (container)
  |     container: Serial | None             # parent container
  |     properties: list[str]                # OPL text
  |
  +-- mobiles: dict[Serial, Mobile]
  |     serial, body, name, x, y, z
  |     notoriety, properties
  |
  +-- containers: dict[Serial, set[Serial]]  # container -> child items
  |
  +-- journal: RingBuffer[JournalEntry]      # system/NPC messages
  |     timestamp, source, text, serial
  |
  +-- target_cursor: TargetState | None      # pending target request
  |     cursor_id, cursor_type
  |
  +-- pending_gump: GumpState | None         # open gump/menu
  |     local_id, server_id, buttons, entries
  |
  +-- map: MapReader                          # terrain/static tile lookup
```

**Key addition vs current**: explicit `target_cursor` and `pending_gump` state. These are the two primary interaction mechanisms in UO, and the current code doesn't track them cleanly.

### Layer 3: Action Primitives

Each primitive is an async function that:
1. Sends packet(s)
2. Waits for expected response (with timeout)
3. Returns success/failure with context

```python
@dataclass
class ActionResult:
    success: bool
    message: str = ""
    data: dict = field(default_factory=dict)  # action-specific return data


# --- Movement ---

async def move_to(ctx, x: int, y: int) -> ActionResult:
    """Pathfind and walk to target. Returns when arrived or stuck."""

async def move_step(ctx, direction: int) -> ActionResult:
    """Single step. Waits for confirm/deny."""


# --- Object Interaction ---

async def use_object(ctx, serial: int) -> ActionResult:
    """Double-click object. Waits for server response (target cursor, gump, etc)."""

async def use_skill(ctx, skill_id: int) -> ActionResult:
    """Use skill by ID. Waits for target cursor or direct result."""


# --- Targeting ---

async def wait_target(ctx, timeout: float = 3.0) -> ActionResult:
    """Wait for target cursor to appear."""

async def target_tile(ctx, x: int, y: int, z: int, graphic: int) -> ActionResult:
    """Respond to target cursor with ground tile."""

async def target_object(ctx, serial: int) -> ActionResult:
    """Respond to target cursor with object/mobile."""

async def cancel_target(ctx) -> ActionResult:
    """Cancel pending target cursor."""


# --- Gump/Menu ---

async def wait_gump(ctx, timeout: float = 3.0) -> ActionResult:
    """Wait for gump to appear. Returns gump data."""

async def gump_respond(ctx, button: int, switches: list[int] = []) -> ActionResult:
    """Click button on open gump."""

async def wait_menu(ctx, timeout: float = 3.0) -> ActionResult:
    """Wait for old-style menu."""

async def menu_respond(ctx, index: int) -> ActionResult:
    """Select option from menu."""


# --- Inventory ---

async def drag_drop(ctx, item_serial: int, target_serial: int,
                    x: int = -1, y: int = -1) -> ActionResult:
    """Lift item and drop onto target (container/ground)."""


# --- Vendor ---

async def context_menu_select(ctx, serial: int, cliloc: int) -> ActionResult:
    """Request context menu and select entry by cliloc."""

async def buy_items(ctx, vendor_serial: int,
                    items: list[tuple[int, int]]) -> ActionResult:
    """Send buy request. items = [(serial, amount), ...]"""

async def sell_items(ctx, vendor_serial: int,
                     items: list[tuple[int, int]]) -> ActionResult:
    """Send sell request."""


# --- Communication ---

async def say(ctx, text: str) -> ActionResult:
    """Say text in-game."""
```

**Key principle**: Primitives are stateless and composable. They don't know about mining or crafting — they just execute packet sequences with proper waiting.

### Layer 4: Procedures

Procedures are state machines that combine primitives into game workflows. Each procedure:
- Has **preconditions** (can I do this right now?)
- Executes a **sequence of primitives** with error handling
- Returns a **result** (success/failure/partial + what happened)
- Handles **interrupts** (attacked, overweight, tool broke)

```python
class Procedure(ABC):
    """Base class for game procedures."""
    name: str

    @abstractmethod
    async def can_execute(self, world: WorldModel) -> bool:
        """Check if preconditions are met."""

    @abstractmethod
    async def execute(self, ctx: ActionContext) -> ProcedureResult:
        """Run the procedure. May be long-running."""

    def priority_score(self, world: WorldModel) -> float:
        """How urgent is this procedure right now? Used by planner."""


@dataclass
class ProcedureResult:
    success: bool
    message: str
    items_gained: list[Item] = field(default_factory=list)
    gold_changed: int = 0
    skill_gains: dict[int, float] = field(default_factory=dict)
    next_suggestion: str | None = None  # hint for planner
```

#### Core Procedures for Miner/Blacksmith:

```python
class MineOre(Procedure):
    """Mine ore from a target tile.

    Flow: use_object(pickaxe) -> wait_target -> target_tile(rock)
          -> wait for journal message -> parse result

    Repeats on same tile until depleted, then returns.
    Handles: tool breaking, vein exhaustion, overweight.
    """

class SmeltOre(Procedure):
    """Smelt all ore in backpack at nearest forge.

    Flow: for each ore stack:
            use_object(ore) -> wait_target -> target_object(forge)
            -> wait for result

    Handles: forge not found (move_to forge first), smelt failure.
    """

class BankDeposit(Procedure):
    """Deposit items at bank.

    Flow: move_to(bank) -> say("bank") -> wait for bankbox
          -> drag_drop(items, bankbox)

    Handles: bank not nearby (move_to), bankbox full.
    """

class CraftItem(Procedure):
    """Craft specific item via crafting gump.

    Flow: use_object(tool) -> wait_gump -> gump_respond(category)
          -> wait_gump -> gump_respond(item) -> wait for result

    Handles: missing materials, skill too low, tool broke.
    """

class SmeltItem(Procedure):
    """Recycle crafted items back to ingots.

    Flow: use_object(tool) -> wait_gump -> gump_respond("Smelt Item")
          -> wait_target -> target_object(crafted_item) -> wait result
    """

class BuyFromVendor(Procedure):
    """Buy items from NPC vendor.

    Flow: move_to(vendor) -> context_menu_select(vendor, BUY)
          -> wait for buy list -> buy_items(vendor, items)

    Handles: no gold, vendor too far, item not available.
    """

class SellToVendor(Procedure):
    """Sell items to NPC vendor.

    Flow: move_to(vendor) -> context_menu_select(vendor, SELL)
          -> wait for sell list -> sell_items(vendor, items)
    """

class MakeTools(Procedure):
    """Craft tools using tinkering.

    Flow: use_object(tinker_tools) -> wait_gump
          -> gump_respond("Tools") -> gump_respond("Pickaxe")

    Precondition: have tinker tools + ingots.
    """

class MiningLoop(Procedure):
    """High-level mining session.

    Combines: MineOre (repeat) -> SmeltOre (when heavy)
              -> BankDeposit (when ingots accumulated)
              -> MakeTools (when tools low)

    This is the "macro" that runs for extended periods.
    Tracks: tiles mined, veins found, ore collected.
    Knows to move to new tile area after 3 mines (anti-macro).
    """

class SmithTraining(Procedure):
    """Blacksmithy training session.

    Selects optimal item for current skill level.
    Crafts items -> smelts failures -> repeat.
    Tracks ingot consumption and adjusts recipe.
    """
```

### Layer 5: Planner

The planner evaluates world state and decides which procedure to run next. It uses **rules first, LLM second**.

```python
class Planner:
    """Decides what to do next based on world state and character goals."""

    async def next_procedure(self, world: WorldModel,
                              character: Character) -> Procedure:
        # Priority 1: Survival
        if world.self.hp_percent < 30:
            return Flee()

        # Priority 2: Respond to speech (social)
        if world.has_pending_speech():
            return RespondToSpeech()

        # Priority 3: Weight management
        if world.self.weight_ratio > 0.85:
            if self._near_forge(world):
                return SmeltOre()
            else:
                return MoveTo(self._nearest_forge(world))

        # Priority 4: Tool management
        if not self._has_tool(world, character.primary_tool):
            if self._has_tinker_tools(world) and self._has_ingots(world):
                return MakeTools(character.primary_tool)
            elif self._near_vendor(world):
                return BuyFromVendor(character.needed_tools)
            else:
                return MoveTo(self._nearest_vendor(world))

        # Priority 5: Primary activity
        if character.primary_activity == "mining":
            if self._near_minable_tile(world):
                return MineOre()
            else:
                return MoveTo(self._nearest_mine(world))

        # Priority 6: Secondary activity (crafting for training/profit)
        if self._should_craft(world, character):
            if self._near_anvil_and_forge(world):
                return CraftItem(self._best_recipe(world, character))
            else:
                return MoveTo(self._nearest_smithy(world))

        # Priority 7: Sell accumulated goods
        if self._should_sell(world):
            return SellToVendor()

        # Priority 8: Bank deposit
        if self._should_bank(world):
            return BankDeposit()

        # Fallback: LLM decision
        return await self._llm_decide(world, character)
```

**Rule-based vs LLM:**
- Rules handle 95% of decisions (deterministic, fast, free)
- LLM handles: "where should I mine next?", "should I train smithing or keep mining?", unexpected situations
- LLM is called only when rules don't produce an answer

**No Q-learning.** The procedures are known. The decision tree is straightforward. If we add learning later, it should be about optimizing parameters (which mine spot yields most ore, which recipe is most efficient for training) — not about discovering procedures.

### Layer 6: Character Definition (Markdown)

Characters are defined as natural Markdown documents. The LLM can read them directly, and humans can edit them comfortably. The parser extracts structured data from headings and lists.

```markdown
<!-- characters/hully.md -->

# Hully

A dusty miner and blacksmith in Minoc. Practical, no-nonsense.
Focused on efficiency and profit. Prefers working alone.

## Skills

### Primary
- Mining: target 100, priority 1
- Blacksmithy: target 100, priority 2

### Support
- Tinkering: target 60 (enough to make tools)

## Activities

- Mining: 60% of time
- Crafting: 20%
- Selling: 10%
- Banking: 10%

## Locations

- Home: Minoc
- Mine: Minoc Mountain
- Forge: Minoc Smithy
- Bank: Minoc Bank
- Vendor: Minoc Weaponsmith
- Tinker: Minoc Tinker

## Economy

### Keep
- pickaxe, tongs, tinker tools, ingots

### Sell
- crafted weapons, crafted armor

### Thresholds
- Min gold reserve: 500
- Bank when carrying 200+ ingots
```

Parser implementation:
```python
@dataclass
class Character:
    name: str
    description: str       # first paragraph under heading
    skills: list[SkillGoal]
    activities: list[Activity]
    locations: dict[str, str]
    keep_items: list[str]
    sell_items: list[str]
    thresholds: dict[str, int]

def load_character(path: Path) -> Character:
    """Parse character markdown into structured data."""
    # Split by headings -> parse lists/key:value pairs
    ...
```

Advantages of Markdown over YAML:
- Natural language descriptions and structured data **coexist in one document**
- Can be **injected directly** into LLM prompts without parsing
- **Intuitive** for humans to read and edit
- Easy to track changes via git diff

---

## 4. Procedure Detail: Mining Loop

This is the most important workflow. Here's the detailed state machine:

```
                    +-------------+
                    |   START     |
                    +------+------+
                           |
                    +------v------+
                    | Check tools |---no tools---> MakeTools / BuyTools
                    +------+------+
                           |
                    +------v------+
                    | At mine?    |---no---------> MoveTo(mine)
                    +------+------+
                           |
                    +------v------+
              +---->| Find tile   |---no tiles---> MoveTo(new area)
              |     +------+------+
              |            |
              |     +------v------+
              |     | Mine tile   |
              |     | use_object  |
              |     | wait_target |
              |     | target_tile |
              |     +------+------+
              |            |
              |     +------v-----------+
              |     | Parse result     |
              |     +--+---+---+---+---+
              |        |   |   |   |
              |   success  |  fail  tool_broke
              |        |   |   |      |
              |        | depleted  MakeTools
              |        |   |
              |  +-----v-+ |   +--------+
              |  | Check | +-->| Next   |
              |  | weight|     | tile   |---+
              |  +---+---+     +--------+   |
              |      |                      |
              |   heavy?                    |
              |      |                      |
              |  +---v-------+              |
              |  | SmeltOre  |              |
              |  +---+-------+              |
              |      |                      |
              |  many ingots?               |
              |      |                      |
              |  +---v--------+             |
              |  | BankDeposit|             |
              |  +---+--------+             |
              |      |                      |
              +------+----------------------+
```

**Anti-macro awareness**: After 3 mines in the same 4x4 area, move to a different area. Track mined locations with timestamps.

**Journal parsing**: The procedure must read journal messages to know what happened:
- `"You dig some [X] ore"` -> success, continue
- `"You loosen some rocks but fail"` -> skill check failed, try again
- `"There is no metal here"` -> vein depleted, move to next tile
- `"That is too far away"` -> need to move closer
- `"Your backpack is full"` -> weight management

---

## 5. Key Differences from Current Architecture

| Aspect | Current (v1) | Proposed (v2) |
|--------|-------------|--------------|
| Decision model | Q-learning + UCB1 | Rule-based planner + LLM fallback |
| Skill execution | Single `execute()` call | Multi-step procedure with state machine |
| Error handling | Check after, return success/fail | Parse journal messages inline, handle each case |
| Target system | Implicit (hidden in skill code) | Explicit `wait_target` + `target_tile` primitives |
| Gump system | Implicit | Explicit `wait_gump` + `gump_respond` primitives |
| Reward signal | Fake positive rewards | No rewards needed — procedures know if they succeeded |
| Weight management | Not tracked | Core part of planner priority system |
| Tool management | `_find_missing_tools` | Planner checks before every activity |
| Anti-macro | Not handled | Track mine locations, rotate every 3 mines |
| Mining location | Hardcoded | Character config + map scanning |

---

## 6. File Organization

```
anima/
  protocol/            # Layer 1: Network
    connection.py       # TCP, reconnect
    packets.py          # Packet builders (outgoing)
    parser.py           # Packet handlers (incoming)
    huffman.py          # Compression

  world/                # Layer 2: World Model
    model.py            # WorldModel dataclass (items, mobiles, self)
    player.py           # PlayerState (stats, skills, equipment)
    items.py            # Item tracking, container hierarchy
    mobiles.py          # Mobile tracking
    journal.py          # Journal message buffer + parsing
    map.py              # Terrain/static tile lookup

  actions/              # Layer 3: Primitives
    movement.py         # move_to, move_step, pathfind
    interact.py         # use_object, use_skill
    target.py           # wait_target, target_tile, target_object
    gump.py             # wait_gump, gump_respond
    inventory.py        # drag_drop, find_item, count_items
    vendor.py           # context_menu, buy_items, sell_items
    speech.py           # say, wait_for_message

  procedures/           # Layer 4: Game Workflows
    base.py             # Procedure ABC, ProcedureResult
    mining.py           # MineOre, MiningLoop
    smelting.py         # SmeltOre, SmeltItem
    crafting.py         # CraftItem, SmithTraining
    banking.py          # BankDeposit, BankWithdraw
    trading.py          # BuyFromVendor, SellToVendor
    tools.py            # MakeTools (tinkering)

  planner/              # Layer 5: Strategic Decisions
    planner.py          # Rule-based priority planner
    state_eval.py       # World state evaluation helpers
    llm_advisor.py      # LLM fallback for complex decisions

  character/            # Layer 6: Persona
    loader.py           # Markdown -> Character dataclass
    character.py        # Character model (skills, goals, locations)

  monitor/              # Observability (existing, keep)
    tui.py
    state_publisher.py

  main.py               # Entry point, connect + run loop

characters/             # Character definitions (Markdown)
  hully.md
  grimm.md
```

---

## 7. Interaction Flow Example

**Input**: "hully is a miner and blacksmith"

**Startup**:
1. Load `characters/hully.md` -> Character(name="Hully", primary=mining, ...)
2. Connect to server, login, enter world
3. Initialize WorldModel from incoming packets
4. Start planner loop

**Tick 1**: Planner evaluates:
- HP OK, no speech, weight OK
- Has pickaxe? Yes (3 in backpack)
- At mine? No (at Minoc bank)
- -> `MoveTo("Minoc Mountain")`

**Tick 2-10**: Walking to mine...

**Tick 11**: Arrived at mine. Planner:
- Has pickaxe? Yes
- At mine? Yes
- Near minable tile? Yes (mountain tiles nearby)
- -> `MineOre(tile=(2501, 540, 15, 0x021A))`

**Tick 12**: MineOre procedure runs:
1. `use_object(pickaxe_serial)` -> send 0x06
2. `wait_target(timeout=3.0)` -> wait for 0x6C
3. `target_tile(2501, 540, 15, 0x021A)` -> send 0x6C response
4. Wait for journal: "You dig some iron ore" -> success!
5. Check weight (42+12=54 stone, under 350) -> continue
6. Repeat on same tile...

**Tick 25**: Journal says "There is no metal here to mine"
- MineOre returns `ProcedureResult(success=True, next_suggestion="move_to_new_tile")`
- Planner picks adjacent tile, runs MineOre again

**Tick 100**: Weight > 350 stone. MineOre returns "overweight".
- Planner priority: weight management -> SmeltOre
- SmeltOre: move to forge (nearby), smelt all ore stacks
- Then continue mining

**Tick 200**: 200+ ingots in backpack.
- Planner: bank threshold reached -> BankDeposit
- Walk to Minoc Bank, say "bank", deposit ingots
- Walk back to mine, continue

---

## 8. AI Evolution Roadmap

AI capabilities should be introduced incrementally. Without accurate data, learning systems
accumulate false rewards — as demonstrated by the buy_from_npc loop that repeated 12,000 times
(the result of attempting Phase 4 before completing Phase 1).

### Phase 1: Rule-Based Execution + Data Collection (Current)

Run on deterministic rules while **recording all action outcomes**.

Data to collect:
- **Mining logs**: tile coordinates, ore type, quantity, timestamp, depletion status
- **Crafting logs**: recipe, skill level, success/failure, materials consumed
- **Sales logs**: item, price, vendor, timestamp
- **Movement logs**: path, travel time, obstacles encountered
- **Tool usage**: uses per tool, breakage points
- **Skill gains**: skill ID, gain timestamp, gain amount, action that triggered it

```python
@dataclass
class ActionLog:
    timestamp: float
    procedure: str          # "mine_ore", "craft_item", etc.
    location: tuple[int, int]
    params: dict            # procedure-specific (tile, recipe, etc.)
    result: str             # "success", "failure", "depleted", etc.
    details: dict           # ore_type, gold_earned, skill_gain, etc.
    duration_ms: float
```

Even simple statistics unlock meaningful optimization:
- "Mine zone A yields 23 ore on average, zone B yields 31" -> prefer zone B
- "Cutlass returns 1.2gp/ingot, plate gorget returns 1.8gp/ingot" -> craft gorgets

### Phase 2: Predictive Models (After Thousands of Records)

Use accumulated data to **optimize parameters**. Not Q-learning — statistics and prediction.

**Ore vein prediction**:
```
"Last mined this zone 14 minutes ago -> respawn probability 87%"
-> Calculate optimal patrol route across zones
-> Model: EMA of (depletion_time, respawn_time) pairs per zone
```

**Crafting optimization**:
```
"Current skill 67.3 -> cutlass success 94%, plate gorget success 23%"
-> Optimize tradeoff: skill gain rate vs material consumption
-> Model: (skill_level, recipe) -> (success_rate, gain_probability) regression
```

**Revenue optimization**:
```
"Plate gorget: 10 ingots, sells for 18gp, success 78% -> expected 1.4gp/ingot"
"Cutlass: 8 ingots, sells for 9gp, success 95% -> expected 1.07gp/ingot"
-> Automatically select highest expected-revenue recipe
```

Techniques used at this phase: EMA (exponential moving average), linear regression,
simple classification. Reinforcement learning is still unnecessary.

### Phase 3: LLM-Driven Autonomy (Strategic Decisions)

Rules + statistical models handle daily routines. **Exceptional situations and long-term
strategy** are delegated to the LLM.

**Situational judgment** (cases not covered by rules):
- "An unknown NPC just spoke to me — ignore? respond? flee?"
- "Discovered a new area — explore or ignore?"
- "A PK appeared — what's the escape route?"

**Long-term planning** (data summary + LLM):
```
Prompt to LLM:
"Last 24 hours:
 - Mining 68.1 -> 68.4 (+0.3/day)
 - Blacksmithy 55.0 -> 55.0 (no change)
 - Mining revenue: 2,400gp
 - Ingots on hand: 1,200
 Current goal: Mining GM (100.0)
 Estimated arrival: 106 days

 If we interleave Blacksmithy training, we consume ingots while
 leveling both skills simultaneously. Should we adjust the schedule?"
```

**Multi-character coordination**:
- "Hully (miner) delivers ingots to Grimm (blacksmith)"
- LLM coordinates schedules and locations between characters

### Phase 4: Fully Autonomous Agent

Data + predictive models + LLM combined into the final form.

```
Input:  "Hully is a miner and blacksmith. A production character
         that will grow to earn resources and gold."

-> LLM reads Character MD and creates long-term plan
-> Planner executes daily routine using rules + statistical data
-> Predictive models suggest optimal routes/timing/recipes
-> LLM handles unexpected situations
-> Result data feeds back into models
-> LLM periodically reviews and adjusts strategy
```

### Phase Summary

```
Phase 1 (now)      Rule-based execution + data collection
                    "Execute known procedures accurately, record everything"
                     |
                     | accumulate data (thousands of records)
                     v
Phase 2             Statistical/predictive models for parameter optimization
                    "Decide where to mine and what to craft based on data"
                     |
                     | stable routine established
                     v
Phase 3             LLM handles strategic decisions + exception handling
                    "Rules handle the routine, LLM handles the unexpected"
                     |
                     | experience accumulated + models refined
                     v
Phase 4             Fully autonomous — just provide goals
                    "Give a character description, the plan-execute-learn loop runs itself"
```

### Why Not Q-Learning (Now)

Q-learning is effective when:
- The optimal strategy is **unknown**
- State-action mappings are **complex**
- **Exploration** is needed to discover rewards

UO production characters meet NONE of these criteria:
- **Strategy is known**: mine -> smelt -> bank -> craft -> sell
- **State-action mapping is simple**: overweight? smelt. no tools? make them.
- **No exploration needed**: procedures are documented and fixed

Why predictive models (Phase 2) are more appropriate than Q-learning:
- Reward signals are **unambiguous** (ore count, gold amount — facts, not estimates)
- State space is **small** (location, skill level, inventory)
- Results are **immediately verifiable** (did ore actually appear in backpack?)

---

## 9. Migration Path

The current codebase has solid foundations in Layer 1 (protocol) and Layer 2 (world model). The migration:

1. **Keep**: protocol/, perception/ (rename to world/)
2. **Refactor**: Extract action primitives from current skill code into actions/
3. **Rewrite**: Replace skills/ with procedures/ — proper state machines
4. **Replace**: Remove Q-learning selector, replace with rule-based planner
5. **Simplify**: Remove behavior tree — the planner IS the decision maker
6. **Keep**: monitor/, TUI, event system — observability is valuable
7. **Add**: ActionLog data collection — foundation for Phase 2
