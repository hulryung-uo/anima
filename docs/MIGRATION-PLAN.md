# Anima v1 → v2 Migration Plan

## Context

The current architecture uses Q-learning + behavior tree for decision making, but this
proved ineffective — false rewards caused the agent to repeat buy_from_npc 12,000 times.
UO gameplay is procedural, not exploratory. The migration replaces Q-learning with
rule-based planning, and refactors monolithic skills into composable layers:
**Action Primitives → Procedures → Planner**.

The agent must stay functional throughout the migration. Each step is independently
testable and committable.

---

## Step 1: AgentContext — typed replacement for blackboard

**Why**: `BrainContext.blackboard` is an untyped dict with 15+ implicit keys
(`skill_problem`, `depleted_mines`, `buy_cooldown_until`, `refused_vendors`, etc.)
spread across 30 files. All subsequent steps need a clean context object.

**Create**:
- `anima/core/context.py` — `AgentContext` dataclass with typed fields

**Modify**:
- `anima/brain/behavior_tree.py` — `BrainContext = AgentContext` (alias for backward compat)
- `anima/core/avatar.py` — construct `AgentContext` instead of raw dict blackboard

**Key files**: `behavior_tree.py:27`, `avatar.py`, `brain.py`, `goals.py` (already has blackboard bridge)

**Test**: All existing tests pass. `BrainContext` alias means zero import changes needed.

---

## Step 2: Action Primitives — extract reusable packet sequences

**Why**: Skills like mine.py, blacksmith.py, vendor.py all contain duplicate
patterns (use_object→wait_target→target_tile, wait_for_gump→click_button).
Extracting these enables composition in procedures.

**Create** (`anima/actions/`):
- `target.py` — `use_on_target(ctx, tool, tile)`, `use_on_object(ctx, tool, target)`, `wait_for_target(ctx)`
- `gump.py` — `wait_for_gump(ctx)`, `click_gump_button(ctx, gump, button)`, `craft_via_gump(ctx, tool, category, item)`
- `vendor.py` — `request_context_menu(ctx, serial, cliloc)`, `wait_for_buy_list(ctx)`, `wait_for_sell_list(ctx)`
- `inventory.py` — `find_in_backpack(ctx, graphics)`, `count_items(ctx, graphics)`, `drag_drop(ctx, item, target)`
- `journal.py` — `wait_for_journal(ctx, patterns, timeout)` — replaces ad-hoc journal polling in skills

**Source patterns extracted from**:
- `skills/gathering/mine.py` lines 100-160 (target_tile pattern)
- `skills/crafting/blacksmith.py` lines 80-150 (gump pattern)
- `skills/trade/vendor.py` lines 74-149 (context menu pattern)

**Existing reusable code**: `action/movement.py` (go_to), `action/interaction.py` (use_item) already exist — keep and reference.

**Test**: Unit tests per primitive. Skills remain unchanged (still use their inline code).

---

## Step 3: Procedure base class + ProcedureRegistry

**Why**: Define the contract for state-machine workflows that replace Skills.

**Create** (`anima/procedures/`):
- `base.py` — `Procedure` ABC, `ProcedureResult`, `ProcedureRegistry`

**Design** (modeled after existing `Skill` ABC at `skills/base.py`):
```python
class ProcedureResult:
    status: Literal["running", "success", "failure"]
    message: str = ""

class Procedure(ABC):
    name: str
    description: str

    async def can_start(self, ctx: AgentContext) -> bool: ...
    async def tick(self, ctx: AgentContext) -> ProcedureResult: ...
    async def diagnose(self, ctx: AgentContext) -> str | None: ...
```

Key difference from `Skill`: `tick()` returns RUNNING (multi-tick state machine) vs
`execute()` which blocks until done. This prevents the stuck-skill problem.

`ProcedureRegistry` mirrors `SkillRegistry` at `skills/base.py:133-165`.

**Test**: Registry tests, ProcedureResult tests.

**Note**: Steps 2 and 3 are independent — can be done in parallel.

---

## Step 4: Migrate skills → procedures (incremental)

**Why**: Convert each skill to a procedure using action primitives. Each procedure
is a separate commit. The agent runs on a mix of old skills and new procedures.

**Migration order** (simplest → most complex):

| # | New Procedure | Old Skill Source | Key Action Primitives |
|---|--------------|-----------------|----------------------|
| 1 | `procedures/mine_ore.py` | `skills/gathering/mine.py` | target.use_on_target, journal.wait_for_journal |
| 2 | `procedures/smelt_ore.py` | `skills/crafting/smelt.py` | target.use_on_object |
| 3 | `procedures/chop_wood.py` | `skills/gathering/lumber.py` | target.use_on_target |
| 4 | `procedures/make_tools.py` | `skills/crafting/tinker.py` | gump.craft_via_gump |
| 5 | `procedures/craft_blacksmith.py` | `skills/crafting/blacksmith.py` | gump.craft_via_gump |
| 6 | `procedures/buy_from_vendor.py` | `skills/trade/vendor.py` BuyFromNpc | vendor.request_context_menu |
| 7 | `procedures/sell_to_vendor.py` | `skills/trade/vendor.py` SellToNpc | vendor.request_context_menu |
| 8 | `procedures/bank_deposit.py` | `skills/trade/banking.py` | inventory.drag_drop |

**Pattern for each migration**:
1. Create `procedures/X.py` — imports action primitives + domain constants from old skill
2. Register in `ProcedureRegistry` (in avatar.py or auto-discover)
3. Test: verify `can_start()` matches old `can_execute()`, workflow produces same result
4. Old skill remains registered — both coexist until Planner (Step 5) takes over

**Adding NEW procedures later** (e.g., fishing):
- Create `procedures/fishing.py` implementing Procedure ABC
- That's it — no planner changes, no registry changes

---

## Step 5: Rule-based Planner (replaces BT + Q-learning)

**Why**: The BT + Q-learning selector in `brain.py:56-220` is the core problem.
Replace with explicit priority rules.

**Create** (`anima/planner/`):
- `planner.py` — `Planner` class with priority-based procedure selection
- `rules.py` — Priority rule definitions
- `llm_advisor.py` — LLM fallback (moved from `brain/think.py`)

**Planner logic** (replaces `brain.py:_skill_action` and `build_default_tree`):
```
Priority 1: Survival (hp < 30% → flee/heal)
Priority 2: Social (pending speech → respond)
Priority 3: Continue running procedure (if one is in RUNNING state)
Priority 4: Weight management (> 85% → smelt or bank)
Priority 5: Tool management (no tools → make or buy)
Priority 6: Primary activity (mine/craft based on character)
Priority 7: Secondary activities (sell, bank)
Priority 8: LLM strategic decision (where to go, what to focus on)
```

**Modify**:
- `main.py` — `brain_loop` calls `planner.tick()` instead of `brain.tick()`
- `core/avatar.py` — wire ProcedureRegistry + Planner instead of SkillRegistry + Brain

**Move** (not rewrite):
- `brain/think.py` LLM logic → `planner/llm_advisor.py`
- Goal/movement handling stays in `core/goals.py` + `action/movement.py`

**Delete** (after planner is stable):
- `skills/selector.py` (Q-learning, already disabled)
- `skills/state.py` (Q-learning state encoder)
- `brain/brain.py` `_skill_action` function

**Test**: Verify planner selects correct procedure given mock world states.

---

## Step 6: Character definition (Markdown)

**Why**: Replace YAML personas with Markdown that includes procedure priorities,
readable by both humans and LLM.

**Create**:
- `anima/character/loader.py` — Markdown parser → `Character` dataclass
- `characters/hully.md`, `characters/grimm.md` — character definitions

**Modify**:
- `planner/planner.py` — use character's procedure priorities for selection
- `anima/persona.py` — `from_markdown()` alternative to YAML

**Backward compat**: YAML personas continue to work.

---

## Step 7: Cleanup and renames

**Only after all above is stable.**

- Optional rename: `anima/perception/` → `anima/world/`
- Optional rename: `anima/client/` → `anima/protocol/`
- Delete old skills that have been fully migrated to procedures
- Delete `brain/behavior_tree.py` (after all `BrainContext` imports updated)
- Add ActionLog data collection (foundation for Phase 2 AI evolution)

---

## Dependency Graph

```
Step 1 (AgentContext)
  |
  +--- Step 2 (Action Primitives)    Step 3 (Procedure base)  [parallel]
  |         |                              |
  |         +--------- Step 4 (Migrate skills → procedures, incremental) ---+
  |                                                                          |
  +------------------------------------------------- Step 5 (Planner) ------+
                                                          |
                                                   Step 6 (Character MD)
                                                          |
                                                   Step 7 (Cleanup)
```

## Verification

After each step:
1. `uv run pytest` — all tests pass
2. `uv run python -m anima` — agent connects, moves, executes actions
3. Check `data/events.jsonl` — actions being logged correctly
4. TUI shows activity — `uv run python -m anima --tui`

After Step 5 (planner):
- Agent should mine → smelt → bank → craft → sell autonomously
- No Q-learning, no false rewards
- LLM fallback works for unhandled situations

## Extensibility Verification

After full migration, adding a new scenario should require ONLY:
- **New procedure**: 1 file in `procedures/` implementing Procedure ABC
- **New action primitive**: 1 file in `actions/`
- **New character**: 1 `.md` file in `characters/`
- **No changes** to planner, registry, or main loop
