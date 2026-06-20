# Action API — the authoritative catalog

Audience: **mutation agents** (and humans) changing `anima/`. This is the
complete inventory of behavior primitives and procedures. Prefer reusing
these over writing new packet code. Import everything from the façade:

```python
from anima.actions import go_to, use_on_target, cast_spell, wait_for_journal
```

## Read-me-first invariants

1. **Every behavior runs as a `Procedure`** (`anima/procedures/base.py`):
   implement `async can_start(ctx) -> bool` and
   `async execute(ctx) -> ProcedureResult`, register it in
   `anima/core/avatar.py`. The base class auto-logs to `action_logs` —
   the supervisor and liveness analysis depend on that.
2. **Never await a server response without a bounded timeout.** The
   planner hard-cancels a procedure after `Planner._PROC_TIMEOUT_S`
   (300s default; set a class attr `timeout_s` for legitimate long
   runners), and a separate liveness watchdog cancels anything that
   makes no progress for 90s and forces fallback activity. A procedure
   that blocks forever = liveness gate collapse = fitness ~0.
3. **Fitness reality** (`foundry/kernel/fitness.py`): the viability gate
   needs ≥30 wire actions/hour of ≥2 distinct kinds. Walking counts and
   is the most reliable action. The walk **deny ratio feeds the loop
   penalty** — don't spam movement into walls.
4. **Starvation breaker**: 3 consecutive failures of the same procedure
   demote it for 120s (`Planner._proc_breaker`). Don't build retry
   loops inside procedures; return a failed `ProcedureResult` and let
   the planner re-select.
5. **Return types differ by layer** (historical; both hierarchies stay):

   | Layer | Returns |
   |---|---|
   | `anima/action/*` (movement, interaction, speech) | `bool` or BT `Status` |
   | `anima/actions/*` (target, vendor, gump, skills, …) | `ActionResult(success, message, data)` |
   | `anima/actions/spells.py` | `CastResult(success, fizzled, no_mana, no_reagents)` |
   | `anima/procedures/*` | `ProcedureResult(success, reason, skill_gains, …)` |

## Movement — `anima/action/movement.py`

- `await go_to(ctx, x, y, interrupt_check=None, exact=False, run=None) -> bool`
  A* pathfinding + step walking. Handles: denied-tile rerouting, door
  opening/traversal, stamina-zero waits, mobile-blocking waits, stuck
  escape (doors → perpendicular detours), waypoint fallback for long
  hauls. **Auto-runs** (0x80 bit, 200ms cadence) on legs ≥8 tiles with
  ≥30% stamina; `run=True/False` forces. Returns False after ~20 failed
  path recalcs or 5 rejected turns (server `Frozen`), or when
  `interrupt_check()` returns True. Long walks: give your procedure a
  generous `timeout_s`.
- `await wander_action(ctx) -> Status` — random local wandering with
  stuck escape. Cheap liveness.

## Targeting — `anima/actions/target.py`

The core UO interaction: double-click a tool → server sends a target
cursor (0x6C) → respond with a tile or object.

- `await use_on_target(ctx, tool_serial, x, y, z, graphic=0, timeout=3.0)`
  — tool on a ground tile (mining a rock, smelting at a forge).
- `await use_on_object(ctx, tool_serial, target_serial, timeout=3.0)`
  — tool on an entity (bandage on self, ore on forge).
- `await wait_for_target(ctx, timeout=3.0)` — cursor arrival;
  `data={"cursor_id", "cursor_type"}`. **Always timeout-bounded** —
  the classic freeze bug is awaiting a cursor that never comes.
- `await target_tile(ctx, cursor_id, x, y, z, graphic=0)` /
  `await target_object(ctx, cursor_id, serial)` / `await cancel_target(ctx)`.

## Interaction — `anima/action/interaction.py`

- `await use_item(ctx, serial)` / `await double_click(ctx, serial)` —
  packet 0x06 (open door, use tool, open container).
- `await drag_to_ground(ctx, item, x, y, z, amount=…)` — 2-tile range.
- `await drag_to_container(ctx, item, container_serial)` — e.g. bank box.
- `await move_item_on_ground(ctx, item, dx, dy)` — walk-and-drag.

## Inventory — `anima/actions/inventory.py`

- `find_in_backpack(ctx, graphics: set[int]) -> list[ItemInfo]` — sync,
  **flat** search (does not recurse into bags inside the pack).
- `count_items(ctx, graphics) -> int`
- `await drag_drop(ctx, serial, amount, target_serial)`

## Vendor — `anima/actions/vendor.py` (+ full procedures below)

- `await request_context_menu(ctx, serial)` → `await
  select_context_menu_entry(ctx, serial, cliloc)` — open Buy/Sell.
- `await wait_for_buy_list(ctx, timeout=3.0)` / `await
  wait_for_sell_list(ctx, timeout=3.0)` — then send
  `build_buy_items` / `build_sell_items` (anima/client/packets.py).

## Gumps — `anima/actions/gump.py`

- `await wait_for_gump(ctx, timeout=3.0)` — first open gump.
- `await click_gump_button(ctx, gump, button_id)`
- `await craft_via_gump(ctx, tool_serial, category_button, item_button)`
  — the crafting two-click (used by blacksmithing).

## Journal — `anima/actions/journal.py`

- `await wait_for_journal(ctx, patterns, timeout=5.0, since=None)` —
  match server text (clilocs are resolved to English). `data={"index",
  "text"}` tells you which pattern hit. This is how success/failure of
  most skill uses is detected. Capture `since = time.time()` BEFORE
  sending the action.

## Skills — `anima/actions/skills.py`

Skill ids = ServUO `SkillName` enum (table in `anima/client/appearance.py`).

- `await use_skill(ctx, skill_id)` — untargeted (Hiding=21, Meditation=46).
  Server lockout: ~10s between active skill uses (`SKILL_USE_COOLDOWN_S`).
- `await use_skill_on(ctx, skill_id, target_serial)` — targeted skills
  (Peacemaking=9, …): UseSkill → cursor → object target.
- `await meditate(ctx, target_pct=90.0, timeout=30.0)` — trance +
  mana polling; gains Meditation either way.

## Spells — `anima/actions/spells.py`

- `await cast_spell(ctx, spell_id, target_serial=None, mana_cost=0,
  cast_delay=2.5) -> CastResult` — **wire ids are 1-based**
  (registry+1): Greater Heal=29, Bless=17. Flow: mana precheck → cast →
  cursor arrival (=cast finished) → target (None=self) → journal
  classification. `fizzled=True` still means skill GAIN — treat as
  success when grinding. Reagents must be loose in the backpack
  (`find_in_backpack` is flat).

## Equip — `anima/actions/equip.py`

- `await equip_item(ctx, serial, layer)` — lift+equip, verified via the
  0x2E equipment update. Layers: 1=right hand, 2=left hand.
- `await equip_weapon_from_pack(ctx, graphics, two_handed=False)`.
- `await equip_shield_from_pack(ctx)` — equip a shield from the pack onto the
  off-hand (layer 2) for the Parrying stream.

## Loot — `anima/actions/loot.py`

- `find_corpses(ctx, max_dist=3) -> list[ItemInfo]` — sync; corpses (graphic
  0x2006) on the ground near the agent, nearest first.
- `await loot_corpse(ctx, corpse_serial)` — open a corpse and lift gold +
  valuables into the backpack; weight-gated (see `data["weight_gated"]`).

## Speech — `anima/action/speech.py` & raw packets

- `await respond_to_speech(ctx)` — persona/LLM reply pipeline.
- One-off lines: `ctx.conn.send_packet(build_unicode_speech("…"))`.
  Speech volume drives the **sociability descriptor**
  (speech_sent/actions, `foundry/kernel/descriptor.py`).

## Procedure catalog — `anima/procedures/`

| name | preconditions (can_start) | what it does / fails with |
|---|---|---|
| `mine_ore` | pickaxe in pack, mineable tile ≤2 tiles | mining tour; `timeout_s=600` |
| `smelt_ore` | ≥2 ore, forge near | ore→ingots |
| `chop_wood` | hatchet, tree near | logs; `timeout_s=600` |
| `make_tools` | tinker tools+ingots | crafts tools via gump |
| `craft_blacksmith` | tongs+ingots, anvil/forge | crafts weapons for sale |
| `buy_from_vendor` | gold, vendor known | context-menu buy flow + verify |
| `sell_to_vendor` | sellables, vendor | sell flow + gold verify |
| `bank_deposit` / `check_bank_balance` | banker near | banking |
| `practice_hiding` | alive | Hiding grind (no items); 10s lockout pacing; after a successful hide, walks slow E/W steps through the lockout to roll Stealth (stops on reveal) |
| `practice_music` | instrument in pack | Musicianship grind; no journal signal — detects via 0x3A skill deltas; speaks ~1/9 plays |
| `practice_peacemaking` | alive, instrument in pack | area-peace on self (creature cursor → own serial): rolls Musicianship AND Peacemaking, no mobs needed; first use answers the instrument cursor; 10s lockout pacing |
| `practice_magery` | spellbook worn/in pack | Greater Heal self-cast grind; meditates when mana low |
| `bandage_self` | bandages, HP <95% | one bandage cycle (~8s); Healing gain |
| `hunt_nearby` | weapon, hostile ≤10 tiles, HP ≥40% | bounded combat loop; retreats <35% HP; always drops war mode; targets wounded-first (focus-fire) |
| `wander_for_combat` | weapon, no hostile in range, HP ≥40% | roam the combat anchor to find hostiles when none are in range (re-engage instead of falling through to mining); yields to other work after a few empty sweeps |

## Planner integration — `anima/planner/planner.py`

- Priority chain: death → low-HP heal/bandage → **profession loop**
  (`PROFESSION_LOOPS`, keyed by `persona.profession`: mage/bard/thief/
  adventurer) → overweight smelt → … mining chain … → LLM fallback.
- A procedure is skipped when: blackboard `_skip_procedures` (10 repeat
  fails), supervisor hint, strategy exclusion, goal forbidding, or the
  starvation breaker is open. All of these auto-expire or are cleared
  by the deadlock resolver — don't fight them, return failure honestly.
- New procedure checklist: subclass `Procedure`, set `name`/
  `description` (+ `timeout_s` if >300s legitimate), register in
  `avatar.py`, add a row to THIS file, and unit-test `can_start` gates
  (`tests/test_profession_procedures.py` has the mock-ctx pattern).
