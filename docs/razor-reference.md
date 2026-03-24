# Razor CE Reference for Anima

Reference data and algorithms extracted from [Razor CE](https://github.com/markdwags/Razor) for use in Anima's AI agent system.

Source: `~/dev/uo/razor/`

---

## 1. Targeting Algorithms

Razor has 4 targeting modes. Anima currently only has "nearest hostile" in `skills/combat/melee.py`.

### Notoriety Types (already in Anima as `NotorietyFlag`)

| Value | Name | Color | Razor Category |
|-------|------|-------|----------------|
| 1 | Innocent | Blue | Friendly |
| 2 | GuildAlly | Green | Friendly |
| 3 | Attackable | Gray | NonFriendly |
| 4 | Criminal | Gray | NonFriendly |
| 5 | Enemy | Orange | NonFriendly |
| 6 | Murderer | Red | NonFriendly |
| 7 | Invulnerable | Yellow | N/A |

### Universal Target Filters

All targeting modes apply these filters before selection:
1. Not self (`serial != player.serial`)
2. Not blessed/invulnerable
3. Not a ghost
4. Within range (default 12 tiles)
5. Not in target filter blacklist
6. Not a friend (configurable)

### Closest Target Algorithm

```python
def find_closest(candidates, player_pos):
    closest, best_dist = None, float("inf")
    for m in candidates:
        dist = euclidean_distance(m.position, player_pos)
        if dist < best_dist:
            best_dist = dist
            closest = m
    return closest
```

Variants: all targets, humanoid-only, monster-only.

### Next/Previous Target (Cycling)

Stateful cycling through sorted target list:
- Maintains persistent index across calls
- Optional alphabetical sorting by name
- Wraps around at list boundaries
- Skips self and previous target

### Random Target

Simple `random.choice(candidates)` from filtered list.

### Beneficial vs Harmful Target Split

When "smart targeting" is enabled:
- **Beneficial target**: used for healing/buff spells (flag=2)
- **Harmful target**: used for damage/debuff spells (flag=1)
- Spells automatically pick the right target category

### Humanoid Detection (Body IDs)

```python
HUMAN_BODIES = {0x0190, 0x0191}  # male, female
GHOST_BODIES = {0x025D, 0x025E}  # male ghost, female ghost
```

### Monster Body IDs

Extensive list in `TargetingClosest.cs` lines 99-111. Key ranges:
```python
MONSTER_BODIES = {
    0x01, 0x02, 0x03, 0x04, 0x07, 0x08, 0x09,
    0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12,
    # ... ~90 body types total
}
```

---

## 2. Packet Handler Gap Analysis (Razor vs Anima)

### Missing in Anima (Server → Client)

| Packet | Name | Priority for AI | Notes |
|--------|------|-----------------|-------|
| 0x27 | LiftReject | **HIGH** | Server denies item pickup — need for drag/drop retry |
| 0x2C | ResurrectionGump | MEDIUM | Death handling |
| 0x6F | TradeRequest | MEDIUM | Secure trade with players |
| 0x7C | SendMenu | **HIGH** | Old-style question menus (some crafting uses these) |
| 0x72 | ServerSetWarMode | MEDIUM | Server forces war mode state |
| 0x76 | ServerChange | LOW | Server migration |
| 0x8C | ServerAddress | LOW | Redirect (handled in login flow) |
| 0xB9 | Features | LOW | Server feature flags |
| 0xBC | ChangeSeason | LOW | Visual only |
| 0xDF | BuffDebuff | MEDIUM | Status effect tracking |
| 0x17 | NewMobileStatus | LOW | Alt mobile status format |
| 0x2D | MobileStatInfo | LOW | Extended stat info |
| 0x4E | PersonalLight | LOW | Visual only |
| 0x4F | GlobalLight | LOW | Visual only |
| 0xAF | DeathAnimation | LOW | Visual only |
| 0xD8 | CustomHouseInfo | LOW | Housing |

**Highest priority**: 0x27 (LiftReject) and 0x7C (SendMenu).

### Already in Anima but NOT in Razor

| Packet | Anima Handler | Notes |
|--------|---------------|-------|
| 0x21 | handle_deny_walk | Walk rejection |
| 0x22 | handle_confirm_walk | Walk confirmation |
| 0x74 | handle_vendor_buy_list | Vendor buy list |
| 0x9E | handle_vendor_sell_list | Vendor sell list |
| 0xDC | handle_opl_info | Object property list hash |

---

## 3. Vendor Trade Packet Sequences

### Buy Flow

```
1. Player opens vendor context menu → selects "Buy"
2. Server sends 0x24 (DisplayBuy) — vendor shop contents
3. Server sends 0x74 (ExtBuyInfo) — pricing details
4. Client sends 0x3B (VendorBuyResponse):
   - Vendor serial (u32)
   - Flag: 0x02
   - Per item: layer_flag 0x1A + serial (u32) + amount (u16)
5. Server confirms, updates shop
```

### Sell Flow

```
1. Player opens vendor context menu → selects "Sell"
2. Server sends 0x9E (VendorSellList):
   - Vendor serial (u32)
   - Per item: serial (u32) + graphic (u16) + hue (u16) + amount (u16) + price (u16) + name
3. Client sends 0x9F (VendorSellResponse):
   - Vendor serial (u32)
   - Item count (u16)
   - Per item: serial (u32) + amount (u16)
```

Anima already implements both flows in `skills/trade/vendor.py`.

---

## 4. Drag/Drop Queue (Item Manipulation)

Razor's `DragDropManager` handles reliable item movement with queuing and retry.

### Key Concepts

- **Lift then Drop**: Every item move is a Lift (0x07) followed by Drop (0x08) or Equip (0x13)
- **Circular queue**: 256-slot ring buffer for pending lifts
- **Timeout**: 2-minute timeout on stuck lifts → auto-drop to backpack
- **Distance check**: Ground items must be within 3 tiles

### Packet Formats

```
LiftRequest (0x07): 7 bytes
  item_serial: u32
  amount: u16

DropRequest (0x08): 14 bytes
  item_serial: u32
  x: i16 (-1 for container)
  y: i16 (-1 for container)
  z: i8 (0 for container)
  container_serial: u32

EquipRequest (0x13): 10 bytes
  item_serial: u32
  layer: u8
  mobile_serial: u32
```

### LiftReject (0x27) — Missing in Anima

When the server rejects a lift:
- Reason byte: 0=cannot lift, 1=out of range, 2=out of sight, 5=already holding
- Must clear pending drag state

---

## 5. Equipment Layer System

Already defined in Anima's `perception/enums.py:Layer`. Key layers for AI:

| Layer | Hex | Name | AI Use |
|-------|-----|------|--------|
| 0x01 | ONE_HANDED | Right hand weapon | Combat gear |
| 0x02 | TWO_HANDED | Left hand / 2H weapon | Combat gear, shields |
| 0x06 | HELM | Head armor | Defense |
| 0x0D | INNER_TORSO | Chest armor | Defense |
| 0x15 | BACKPACK | Player backpack | Inventory root |
| 0x19 | MOUNT | Mount | Travel speed |
| 0x1A | SHOP_BUY | Vendor buy container | Trading |
| 0x1D | BANK | Bank box | Storage |

### Two-Handed Weapon Detection

From Razor's `Item.cs:IsTwoHanded`. Most Layer.LeftHand (0x02) items are two-handed except shields.

```python
# Shield ItemID ranges (NOT two-handed despite being Layer 0x02)
SHIELD_IDS = set(range(0x1B72, 0x1B7C))  # standard shields
VIRTUE_SHIELD_IDS = {0x1BC3, 0x1BC4, 0x1BC5}

# Specific RightHand (Layer 0x01) items that ARE two-handed
TWO_HANDED_RIGHT = {
    0x13FC, 0x13FD,  # Heavy Crossbow
    0x13AF, 0x13B2,  # War Axe & Bow
    0x1438, 0x1439,  # War Hammer
    0x1442, 0x1443,  # Two-Handed Axe
    0x1402, 0x1403,  # Short Spear
    0x26C1, 0x26CB,  # AoS Blade
    0x26C2, 0x26CC,  # AoS Bow
    0x26C3, 0x26CD,  # AoS Crossbow
} | set(range(0x0F43, 0x0F51))  # Axes & Crossbow range

def is_two_handed(item_id: int, layer: int) -> bool:
    if layer == 0x02:  # LeftHand
        return item_id not in SHIELD_IDS and item_id not in VIRTUE_SHIELD_IDS
    if layer == 0x01:  # RightHand
        return item_id in TWO_HANDED_RIGHT
    return False
```

**Equip conflict resolution** (from `Dress.cs`, `DressList.cs`):
1. If equipping to RightHand and LeftHand has a 2H item → unequip LeftHand first
2. If equipping to LeftHand and RightHand has a 2H item → unequip RightHand first
3. If equipping any 2H weapon → unequip the opposite hand
4. Before casting Magery/Mysticism spells → may need to unequip weapon

### Item Type Classification

Useful item graphic ID checks from Razor's `Item.cs`:

```python
# Item type detection by graphic ID
def is_container(gfx: int) -> bool:
    return gfx in {0x0E75, 0x0E76, 0x0E77, 0x0E78, 0x0E79,  # pouches/bags
                   0x0E7A, 0x09B0, 0x09B2, 0x09AB, 0x09AA}  # more containers

def is_potion(gfx: int) -> bool:
    return 0x0F06 <= gfx <= 0x0F0D or gfx in {0x2790, 0x27DB}

def is_resource(gfx: int) -> bool:
    return gfx in {
        0x19B7, 0x19B8, 0x19B9, 0x19BA,  # ore
        0x09CC, 0x09CD, 0x09CE, 0x09CF,  # fish
        0x1BDD, 0x1BE0,                   # logs
        0x1779,                           # stone
    }
```

### OPL (Object Property List) Parsing

OPL data arrives via packet 0xD6 (already handled by Anima as `handle_mega_cliloc`).

**OPL packet format:**
```
serial: u32
unknown: u8, u8
hash: i32
properties: [
    cliloc_number: i32  (0 = end)
    args_length: i16    (bytes)
    args: unicode_be     (tab-separated substitution values)
]
```

**Common cliloc patterns for item evaluation:**
- `1042971` / `1070722`: `~1_NOTHING~` (item name)
- `1063483`: `~1_MATERIAL~ ~2_ITEMNAME~` (e.g., "iron longsword")
- `1060847`: `~1_val~ ~2_val~` (generic value pair)

Args use `\t` (tab) as separator for multiple values, substituted into `~1_val~`, `~2_val~` etc.

---

## 6. Spell Data

Full spell database created at `anima/core/spells.py`. Includes:
- 64 Magery spells (circles 1-8)
- 17 Necromancy spells
- 10 Chivalry spells
- 6 Bushido spells
- 8 Ninjitsu spells
- 16 Spellweaving spells
- 16 Mysticism spells

Usage:
```python
from anima.core.spells import get_spell, healing_spells, harmful_spells, SpellSchool

# Get a specific spell
heal = get_spell(4)  # Heal (circle 1, spell 4)

# Find all harmful magery spells
attacks = harmful_spells(SpellSchool.MAGERY)

# Find healing options
heals = healing_spells()
```

Casting via packet:
```python
from anima.client.packets import build_cast_spell
await conn.send_packet(build_cast_spell(spell.id))
```

---

## 7. Gump System

Anima already has a comprehensive gump parser in `perception/gump.py`.

### Key additions from Razor reference

**GumpResponse packet (0xB1) structure:**
```
serial: u32           — gump sender serial
gump_type_id: u32    — gump type ID
button_id: i32        — clicked button (0 = close)
switch_count: i32     — number of active switches
switches: [i32]       — active switch IDs
text_entry_count: i32 — number of text entries
text_entries: [{id: u16, length: u16, text: unicode}]
```

**Gump close packet:** 0xBF subcommand 0x04: `CloseGump(gump_type_id, button_id)`

Anima already builds gump responses via `build_gump_response()` in `packets.py`.
