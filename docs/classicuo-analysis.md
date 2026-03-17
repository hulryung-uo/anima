# ClassicUO Client Analysis for Anima

> Reference analysis of ClassicUO (C#) internals for building a headless Python UO client.
> Graphics/rendering details are excluded — focus is on protocol, state management, and game logic.

---

## 1. Network Layer

### 1.1 Connection Architecture

- **TCP socket** (also supports WebSocket for web-based servers)
- **Two-phase login**: Connection 1 (account login) → Connection 2 (game server)
- **Buffers**: `CircularBuffer` for incoming/outgoing data queues
- **No encryption needed for servuo-rs** (ClassicUO supports Blowfish/Twofish but servuo-rs has no encryption)

### 1.2 Packet Format

All values are **Big-Endian**.

| Type | Format |
|---|---|
| Fixed-length | `[1 byte ID] [payload]` |
| Variable-length | `[1 byte ID] [2 byte length BE] [payload]` |

Length table: `PacketsTable` maps packet ID → fixed length (or -1 for variable).

### 1.3 Compression

| Phase | Server→Client | Client→Server |
|---|---|---|
| Login (Connection 1) | No compression | No compression |
| Game (Connection 2) | **Huffman compressed** | No compression |

Huffman uses a static 257-entry table. Must be implemented for game phase.

### 1.4 Game Loop (Packet Processing)

```
Each frame:
  1. socket.CollectAvailableData()     — non-blocking recv
  2. [Decompress if game mode]         — Huffman
  3. PacketHandlers.ParsePackets()     — split into individual packets
  4. For each packet → lookup handler → call handler(world, data)
  5. socket.Flush()                    — send queued outgoing packets
```

---

## 2. Entity Model

### 2.1 Class Hierarchy

```
LinkedObject          — doubly-linked list node (for container iteration)
  └── BaseGameObject  — base for all game objects
        └── GameObject    — has position (X, Y, Z), Graphic, Hue
              └── Entity      — has Serial, Name, Hits, Flags, Items list
                    ├── Mobile      — NPCs, monsters, players
                    │   └── PlayerMobile  — player character (skills, buffs, gold)
                    └── Item        — ground items, equipment, containers
```

### 2.2 World State (`World.cs`)

Central manager for all game state:

```python
# Python equivalent of ClassicUO's World
class World:
    items: dict[int, Item]          # serial → Item
    mobiles: dict[int, Mobile]      # serial → Mobile
    player: PlayerMobile            # the AI's own character
    map_index: int                  # current map (0=Felucca, 1=Trammel, ...)

    def get(serial) -> Entity       # lookup by serial (checks both dicts)
    def get_or_create_item(serial)  # creates if missing
    def get_or_create_mobile(serial)
    def remove_item(item)           # removes item + contents recursively
    def remove_mobile(mobile)       # removes mobile + items
```

Update cycle: each frame, remove entities beyond `ClientViewRange` (typically 24 tiles).

### 2.3 Serial System

- **32-bit unique ID** for every entity
- `serial >= 0x40000000` → Item
- `serial < 0x40000000` → Mobile
- `0x00000000` = invalid
- `0xFFFFFFFF` = system/server message source

### 2.4 Mobile (`Mobile.cs`)

```python
@dataclass
class Mobile:
    serial: int
    name: str
    graphic: int              # body type (human, animal, monster)
    x: int; y: int; z: int
    direction: Direction
    hue: int

    # Stats
    hits: int; hits_max: int
    mana: int; mana_max: int
    stamina: int; stamina_max: int
    strength: int; dexterity: int; intelligence: int

    # State
    notoriety: NotorietyFlag  # Innocent/Ally/Criminal/Enemy/Murderer
    flags: Flags              # Hidden, Poisoned, Frozen, WarMode, etc.
    in_war_mode: bool
    is_dead: bool
    race: RaceType            # Human/Elf/Gargoyle

    # Movement
    steps: deque[Step]        # queued movement steps
    is_running: bool

    # Equipment (linked list of items)
    items: list[Item]         # equipped items, accessible by Layer
```

### 2.5 PlayerMobile (extends Mobile)

```python
@dataclass
class PlayerMobile(Mobile):
    # Skills
    skills: list[Skill]       # all character skills

    # Resources
    gold: int
    weight: int; weight_max: int
    followers: int; followers_max: int

    # Combat
    damage_min: int; damage_max: int
    physical_resistance: int
    fire_resistance: int; cold_resistance: int
    poison_resistance: int; energy_resistance: int

    # Stat locks
    str_lock: Lock            # Up/Down/Locked
    dex_lock: Lock
    int_lock: Lock

    # Buffs
    buff_icons: dict[BuffIconType, BuffIcon]

    # Movement
    walker: WalkerManager
```

### 2.6 Item (`Item.cs`)

```python
@dataclass
class Item:
    serial: int
    graphic: int
    x: int; y: int; z: int
    amount: int               # stack count
    hue: int
    layer: Layer              # equipment slot (if equipped)
    container: int            # serial of parent container (0xFFFFFFFF = on ground)
    flags: Flags

    # Container contents (if this item is a container)
    items: list[Item]

    @property
    def on_ground(self) -> bool:
        return self.container == 0xFFFFFFFF or self.container == 0

    @property
    def is_corpse(self) -> bool:
        return self.graphic == 0x2006
```

### 2.7 Key Enums

```python
class NotorietyFlag(IntEnum):
    Unknown = 0x00
    Innocent = 0x01      # blue — attackable with penalty
    Ally = 0x02          # green — guild/alliance ally
    Gray = 0x03          # gray — attackable
    Criminal = 0x04      # gray — committed crime
    Enemy = 0x05         # orange — enemy guild
    Murderer = 0x06      # red — player killer
    Invulnerable = 0x07  # yellow — cannot be harmed

class Flags(IntFlag):
    Frozen = 0x01        # paralyzed
    Female = 0x02
    Poisoned = 0x04      # also Flying in newer versions
    YellowBar = 0x08
    IgnoreMobiles = 0x10
    Movable = 0x20
    WarMode = 0x40
    Hidden = 0x80

class Direction(IntEnum):
    North = 0; Right = 1; East = 2; Down = 3
    South = 4; Left = 5; West = 6; Up = 7
    Mask = 0x07
    Running = 0x80       # OR flag for running

class Layer(IntEnum):
    OneHanded = 0x01; TwoHanded = 0x02
    Shoes = 0x03; Pants = 0x04; Shirt = 0x05
    Helmet = 0x06; Gloves = 0x07; Ring = 0x08
    Necklace = 0x0A; Waist = 0x0C; Torso = 0x0D
    Bracelet = 0x0E; Arms = 0x13; Cloak = 0x14
    Backpack = 0x15; Robe = 0x16; Mount = 0x19
    ShopBuy = 0x1B; ShopSell = 0x1C; Bank = 0x1D

class Lock(IntEnum):
    Up = 0; Down = 1; Locked = 2
```

---

## 3. Movement System

### 3.1 Movement Flow

```
Client                                Server
  │                                     │
  │  Send_WalkRequest (0x02)            │
  │  [direction, seq, run, fastwalk]    │
  │ ──────────────────────────────────→ │
  │                                     │
  │     ConfirmWalk (0x22) [seq, noto]  │
  │ ←────────────────────────────────── │
  │   OR                                │
  │     DenyWalk (0x21) [seq, x,y,z,dir]│
  │ ←────────────────────────────────── │
```

### 3.2 WalkerManager

Tracks pending movement steps and handles server responses.

```python
class WalkerManager:
    walk_sequence: int = 0        # 1-255, wraps (never 0)
    steps: list[StepInfo]         # max 5 pending steps
    unaccepted_count: int = 0
    walking_failed: bool = False
    last_step_time: int = 0       # throttle control
    fast_walk_stack: list[int]    # max 5 anti-cheat keys

    def confirm_walk(self, seq: int):
        """Server confirmed step — update position"""
        # Find matching step by sequence
        # Update player world position
        # Dequeue confirmed steps

    def deny_walk(self, seq: int, x: int, y: int, z: int):
        """Server denied step — reset position"""
        # Clear all pending steps
        # Reset player to server-provided position
```

### 3.3 Walk Request Packet (0x02)

```
Byte 0:    0x02 (packet ID)
Byte 1:    direction | (0x80 if running)
Byte 2:    sequence number (1-255)
Bytes 3-6: fast walk key (4 bytes BE)
Total: 7 bytes fixed
```

### 3.4 Sequence Number

- Starts at 1, increments per step, wraps 255 → 1 (never 0)
- Server responds with matching sequence
- Mismatch → resync (send 0x22 resync packet)

### 3.5 Movement Speed Constants

| Mode | Delay (ms/tile) |
|---|---|
| Walk (unmounted) | 400 |
| Run (unmounted) | 200 |
| Walk (mounted) | 200 |
| Run (mounted) | 100 |

### 3.6 Pathfinding (A*)

ClassicUO includes full A* pathfinding in `Pathfinder.cs`:

- **Open/Closed lists**: max 10,000 nodes each
- **Heuristic**: Chebyshev distance
- **Cost**: diagonal = 2, cardinal = 1
- **Z-calculation**: handles surfaces, bridges, impassable, step heights
- **Special modes**: dead/GM (ignore obstacles), sea horse (water), flying (gargoyle)

---

## 4. Combat System

### 4.1 Attack Flow

```
Client                                Server
  │                                     │
  │  Send_AttackRequest (0x05)          │
  │  [target serial]                    │
  │ ──────────────────────────────────→ │
  │                                     │
  │     Swing (0x2F)                    │
  │     [attacker, defender]            │
  │ ←────────────────────────────────── │
  │                                     │
  │     Damage (0x0B)                   │
  │     [serial, damage]                │
  │ ←────────────────────────────────── │
  │                                     │
  │     UpdateHitpoints (0xA1)          │
  │     [serial, hits, hitsMax]         │
  │ ←────────────────────────────────── │
```

### 4.2 Key Combat Packets

| Packet | Dir | Fields | Purpose |
|---|---|---|---|
| 0x05 | C→S | serial | Request attack on target |
| 0x0B | S→C | serial, damage | Damage dealt |
| 0x2F | S→C | attacker, defender | Swing/attack confirmation |
| 0x2C | S→C | action | Death screen (1=resurrect) |
| 0xAF | S→C | serial | Display death of mobile |
| 0x72 | S→C | warMode | War/peace mode state |
| 0xA1 | S→C | serial, hits, hitsMax | HP update |
| 0xA2 | S→C | serial, mana, manaMax | Mana update |
| 0xA3 | S→C | serial, stam, stamMax | Stamina update |
| 0x11 | S→C | (complex) | Full character status |

### 4.3 Targeting System (`TargetManager`)

Some actions require targeting (skills, spells, etc.):

```
Server sends TargetCursor (0x6C):
  - cursor_target: Object(0) / Position(1)
  - cursor_id: uint32
  - target_type: Neutral(0) / Harmful(1) / Beneficial(2)

Client responds with one of:
  - Send_TargetObject (0x6C): serial of target entity
  - Send_TargetXYZ (0x6C): ground coordinates
  - Send_TargetCancel (0x6C): cancel targeting
```

### 4.4 War Mode

- Toggle between peace/war mode
- Must be in war mode to attack innocent players
- Server sends 0x72 with boolean state

---

## 5. Speech System

### 5.1 Sending Speech

| Packet | Purpose | Key Fields |
|---|---|---|
| 0x03 | ASCII speech | type, hue, font, text |
| 0xAD | Unicode speech | type, hue, font, language, text |

### 5.2 Receiving Speech

| Packet | Purpose | Key Fields |
|---|---|---|
| 0x1C | ASCII talk | serial, graphic, type, hue, font, name(30), text |
| 0xAE | Unicode talk | serial, graphic, type, hue, font, lang(4), name, text |

### 5.3 Message Types

```python
class MessageType(IntEnum):
    Regular = 0     # normal speech
    System = 1      # system message
    Emote = 2       # *emote*
    Label = 6       # item/mobile label
    Whisper = 8     # whisper (short range)
    Yell = 9        # yell (long range)
    Spell = 10      # spell words
    Guild = 13      # guild chat
    Alliance = 14   # alliance chat
    Command = 15    # GM command
    Encoded = 0xC0  # keyword-encoded speech
```

### 5.4 Journal

ClassicUO maintains a journal log (`JournalManager`):
- Max 2000 entries in memory
- Each entry: text, font, hue, name, time, message type
- Optional file logging with timestamps

---

## 6. Skills System

### 6.1 Skill Data

```python
@dataclass
class Skill:
    index: int
    name: str
    value: float          # current value (stored as int × 10)
    base: float           # base value
    cap: float            # maximum value
    lock: Lock            # Up/Down/Locked
    is_clickable: bool    # can be used directly
```

### 6.2 Skill Update Packet (0x3A)

| Type byte | Meaning |
|---|---|
| 0x00 | Full skill list (with cap) |
| 0x02 | Full skill list (no cap) |
| 0xDF | Single skill update (with cap) |
| 0xFF | Single skill update (no cap) |
| 0xFE | Skill definitions (names + clickable flags) |

Per-skill data: `id(2) | realValue(2) | baseValue(2) | lock(1) | [cap(2)]`

### 6.3 Using Skills

```
Client sends: 0x12 (Use Skill)
  → sub-command 0x24 | skill_index (ASCII) | " 0"

If skill requires target:
  Server sends: 0x6C (Target Cursor)
  Client responds: 0x6C (Target Object/XYZ/Cancel)
```

---

## 7. Trade System

### 7.1 Player-to-Player Secure Trade

```
Server: 0x6F type=0 → Open trade window (serial, id1, id2, name)
  Players add/remove items by dragging
Server: 0x6F type=2 → Update acceptance state
Server: 0x6F type=3/4 → Update gold amounts
Client: 0x6F → Accept/Decline response
Server: 0x6F type=1 → Close trade
```

### 7.2 NPC Vendor Buy/Sell

**Buy from vendor:**
```
Server: 0x74 (BuyList) → container serial + [price, name] per item
Client: 0x3B (BuyRequest) → vendor serial + [serial, count] per item
```

**Sell to vendor:**
```
Server: 0x9E (SellList) → vendor serial + [serial, graphic, hue, amount, price, name]
Client: 0x9F (SellRequest) → vendor serial + [serial, count] per item
```

---

## 8. Inventory System

### 8.1 Container Management

| Packet | Dir | Purpose |
|---|---|---|
| 0x24 | S→C | Open container (serial, gump graphic) |
| 0x25 | S→C | Update single item in container |
| 0x3C | S→C | Update multiple items in container |
| 0x27 | S→C | Deny item move (drag rejected) |
| 0x29 | S→C | Drop item accepted |

### 8.2 Item Manipulation

| Packet | Dir | Purpose |
|---|---|---|
| 0x07 | C→S | Pick up item (serial, amount) |
| 0x08 | C→S | Drop item (serial, x, y, z, container) |
| 0x13 | C→S | Equip item request |

### 8.3 Container Hierarchy

Items can be nested: `Mobile.Backpack → Bag → Potion`

Finding items recursively:
```python
def find_item_recursive(container: Item, graphic: int) -> Item | None:
    for item in container.items:
        if item.graphic == graphic:
            return item
        if item.items:  # has sub-items (is a container)
            found = find_item_recursive(item, graphic)
            if found:
                return found
    return None
```

---

## 9. Packet Handler Summary (For Headless AI Bot)

### Must Implement (Phase 0-1)

| ID | Name | Category | Purpose |
|---|---|---|---|
| 0x1B | LoginConfirm | LOGIN | Enter world — serial, position |
| 0x55 | LoginComplete | LOGIN | Login sequence done |
| 0xA8 | ServerList | LOGIN | Available servers |
| 0x8C | ServerRedirect | LOGIN | Redirect to game server |
| 0xA9 | CharacterList | LOGIN | Character selection |
| 0x82 | LoginDenied | LOGIN | Login failed |
| 0x20 | UpdatePlayer | MOVEMENT | Position correction |
| 0x21 | DenyWalk | MOVEMENT | Movement rejected |
| 0x22 | ConfirmWalk | MOVEMENT | Movement accepted |
| 0x77/0x78 | UpdateCharacter/Object | ENTITY | Mobile creation/update |
| 0x1D | DeleteObject | ENTITY | Entity removed |
| 0x1A | UpdateItem | ENTITY | Item on ground |
| 0xF3 | UpdateItemSA | ENTITY | Item (new format) |
| 0x1C | Talk | SPEECH | ASCII speech |
| 0xAE | UnicodeTalk | SPEECH | Unicode speech |
| 0xA1 | UpdateHitpoints | COMBAT | HP update |
| 0xA2 | UpdateMana | COMBAT | Mana update |
| 0xA3 | UpdateStamina | COMBAT | Stamina update |
| 0x11 | CharacterStatus | COMBAT | Full status |
| 0x3A | UpdateSkills | SKILL | Skill values |
| 0x2E | EquipItem | INVENTORY | Equipment update |
| 0xB9 | SupportedFeatures | LOGIN | Feature flags |
| 0x73 | Ping | SYSTEM | Keepalive |
| 0xBD | ClientVersion | SYSTEM | Version handshake |

### Should Implement (Phase 2-3)

| ID | Name | Category | Purpose |
|---|---|---|---|
| 0x0B | Damage | COMBAT | Damage numbers |
| 0x2F | Swing | COMBAT | Attack animation |
| 0xAA | AttackCharacter | COMBAT | Attack confirmation |
| 0x2C | DeathScreen | COMBAT | Player death |
| 0xAF | DisplayDeath | COMBAT | Other's death |
| 0x72 | WarMode | COMBAT | War/peace toggle |
| 0xDF | BuffDebuff | COMBAT | Buff/debuff effects |
| 0x24 | OpenContainer | INVENTORY | Container opened |
| 0x25 | UpdateContainedItem | INVENTORY | Item in container |
| 0x3C | UpdateContainedItems | INVENTORY | Multiple items |
| 0x27 | DenyMoveItem | INVENTORY | Drag rejected |
| 0x29 | DropItemAccepted | INVENTORY | Drop confirmed |
| 0x6C | TargetCursor | SYSTEM | Targeting request |
| 0x98 | UpdateName | ENTITY | Name update |

### Should Implement (Phase 4+)

| ID | Name | Category | Purpose |
|---|---|---|---|
| 0x74 | BuyList | TRADE | Vendor stock |
| 0x9E | SellList | TRADE | Vendor buy-back |
| 0x6F | SecureTrading | TRADE | Player trade |
| 0xB0 | OpenGump | UI | Generic gump (menus) |
| 0xDD | CompressedGump | UI | Compressed gump |
| 0xBF | ExtendedCommand | MISC | Context menu, etc. |
| 0x65 | SetWeather | MAP | Weather info |
| 0xBC | Season | MAP | Season info |

### Skip (Graphics/Audio only)

| IDs | Purpose |
|---|---|
| 0x70, 0xC0, 0xC7 | Visual effects |
| 0x6E, 0xE2 | Character animations |
| 0x54 | Sound effects |
| 0x6D | Music |
| 0x4E, 0x4F | Lighting |

---

## 10. Outgoing Packets Summary (AI Bot Actions)

| Packet | Purpose | Format |
|---|---|---|
| 0xEF | Seed | seed(4) + version(4×4) = 21 bytes |
| 0x80 | AccountLogin | user(30) + pass(30) + key(1) = 62 bytes |
| 0x91 | GameLogin | auth(4) + user(30) + pass(30) = 65 bytes |
| 0xA0 | ServerSelect | index(2) = 3 bytes |
| 0x5D | PlayCharacter | name + slot(4) + ip(4) = 73 bytes |
| 0x02 | Walk | dir(1) + seq(1) + fastwalk(4) = 7 bytes |
| 0x05 | Attack | target_serial(4) = 5 bytes |
| 0x06 | DoubleClick | serial(4) = 5 bytes |
| 0x09 | SingleClick | serial(4) = 5 bytes |
| 0x07 | PickUp | serial(4) + amount(2) = 7 bytes |
| 0x08 | Drop | serial(4) + x(2) + y(2) + z(1) + container(4) |
| 0x03 | ASCIISpeech | type(1) + hue(2) + font(2) + text |
| 0xAD | UnicodeSpeech | type(1) + hue(2) + font(2) + lang(4) + text |
| 0x12 | UseSkill | sub(1) + skill_idx(ASCII) + " 0" |
| 0x6C | TargetResponse | cursor_id(4) + type(1) + serial/xyz |
| 0x3B | BuyRequest | vendor(4) + items list |
| 0x9F | SellRequest | vendor(4) + count(2) + items |
| 0x6F | TradeResponse | code + serial + state |
| 0x73 | Ping | seq(1) = 2 bytes |
| 0xB1 | GumpResponse | serial(4) + gump_id(4) + button(4) + switches + text |
| 0x72 | WarMode | flag(1) = 5 bytes |
| 0x34 | StatusRequest | type(1) + serial(4) = 10 bytes |
