# Anima Implementation Plan

> Concrete implementation plan based on ClassicUO and servuo-rs analysis.

---

## 1. Python Module Mapping

Based on ClassicUO's architecture, mapped to our Python package structure:

```
ClassicUO (C#)                    →  Anima (Python)
─────────────────────────────────────────────────────────
Network/NetClient.cs              →  anima/client/connection.py
Network/PacketsTable.cs           →  anima/client/packets.py
Network/OutgoingPackets.cs        →  anima/client/packets.py
Network/PacketHandlers.cs         →  anima/client/parser.py
Network/Huffman.cs                →  anima/client/codec.py
IO/StackDataWriter.cs             →  anima/client/codec.py (struct.pack)
IO/StackDataReader.cs             →  anima/client/codec.py (struct.unpack)

Game/World.cs                     →  anima/perception/world_state.py
Game/GameObjects/Mobile.cs        →  anima/perception/world_state.py
Game/GameObjects/PlayerMobile.cs  →  anima/perception/self_state.py
Game/GameObjects/Item.cs          →  anima/perception/world_state.py
Game/Data/Skill.cs                →  anima/perception/self_state.py

Game/Managers/WalkerManager.cs    →  anima/action/movement.py
Game/Pathfinder.cs                →  anima/action/movement.py
Game/Managers/TargetManager.cs    →  anima/action/combat.py
Game/Managers/JournalManager.cs   →  anima/perception/social_state.py
Game/Managers/MessageManager.cs   →  anima/perception/social_state.py
```

---

## 2. Implementation Phases (Detailed)

### Phase 0-A: Connection & Login (1-2 days)

**Goal**: Connect to servuo-rs, complete full login, enter world.

**Files to create:**

```
anima/
├── __init__.py
├── main.py                    # entry point — connect and login
├── client/
│   ├── __init__.py
│   ├── connection.py          # UoConnection: TCP + login state machine
│   ├── codec.py               # PacketWriter, PacketReader, Huffman
│   └── packets.py             # packet definitions + length table
```

**`connection.py` — Core connection class:**
```python
class UoConnection:
    """Manages TCP connection and login flow."""

    async def connect(self, host: str, port: int)
    async def login(self, username: str, password: str) -> LoginResult
    # Internally handles the two-connection flow:
    #   Connection 1: Seed → AccountLogin → ServerList → ServerSelect → Redirect
    #   Connection 2: Seed → GameLogin → CharacterList → PlayCharacter → LoginConfirm
    async def send_packet(self, packet_id: int, data: bytes)
    async def recv_packet(self) -> tuple[int, bytes]  # (packet_id, payload)
```

**`codec.py` — Binary encoding/decoding:**
```python
class PacketWriter:
    """Build outgoing packets (Big-Endian)."""
    def write_u8(self, v: int)
    def write_u16(self, v: int)        # big-endian
    def write_u32(self, v: int)        # big-endian
    def write_ascii(self, s: str, length: int)  # fixed-length null-padded
    def to_bytes(self) -> bytes

class PacketReader:
    """Parse incoming packets (Big-Endian)."""
    def read_u8(self) -> int
    def read_u16(self) -> int
    def read_u32(self) -> int
    def read_i8(self) -> int
    def read_ascii(self, length: int) -> str
    def read_unicode(self) -> str
    def skip(self, n: int)
    @property
    def remaining(self) -> int

def huffman_decompress(data: bytes) -> bytes:
    """Decompress Huffman-encoded server data."""
```

**`packets.py` — Packet definitions:**
```python
# Packet length table (from servuo-rs PACKET_LENGTHS)
PACKET_LENGTHS: dict[int, int] = {
    0x02: 7,    # WalkRequest
    0x05: 5,    # Attack
    0x06: 5,    # DoubleClick
    0x11: 0,    # CharacterStatus (variable)
    0x1A: 0,    # UpdateItem (variable)
    0x1B: 37,   # LoginConfirm
    0x1C: 0,    # Talk (variable)
    0x1D: 5,    # DeleteObject
    0x20: 19,   # UpdatePlayer
    0x21: 8,    # DenyWalk
    0x22: 3,    # ConfirmWalk
    0x55: 1,    # LoginComplete
    0x73: 2,    # Ping
    0x77: 17,   # UpdateCharacter
    0x78: 0,    # UpdateObject (variable)
    0x80: 62,   # AccountLogin
    0x8C: 11,   # ServerRedirect
    0x91: 65,   # GameLogin
    0xA0: 3,    # ServerSelect
    0xA8: 0,    # ServerList (variable)
    0xA9: 0,    # CharacterList (variable)
    0xAE: 0,    # UnicodeTalk (variable)
    0xB9: 5,    # SupportedFeatures
    0xEF: 21,   # Seed
    # ... (port full table from servuo-rs)
}

# Outgoing packet builders
def build_seed(seed: int, major: int, minor: int, rev: int, patch: int) -> bytes
def build_account_login(username: str, password: str) -> bytes
def build_game_login(auth_key: int, username: str, password: str) -> bytes
def build_server_select(index: int) -> bytes
def build_play_character(name: str, slot: int) -> bytes
def build_walk_request(direction: int, seq: int, fastwalk: int) -> bytes
```

**`main.py` — Verification script:**
```python
async def main():
    conn = UoConnection()
    result = await conn.login("admin", "admin", host="127.0.0.1", port=2593)
    print(f"Logged in as serial=0x{result.serial:08X} at ({result.x}, {result.y}, {result.z})")

    # Listen for packets and print
    while True:
        packet_id, data = await conn.recv_packet()
        print(f"Received packet 0x{packet_id:02X} ({len(data)} bytes)")
```

---

### Phase 0-B: Packet Loop & Basic Movement (1-2 days)

**Goal**: Process game packets, walk around.

**Extend `connection.py`:**
```python
class UoConnection:
    # Add game loop
    async def game_loop(self, handler: PacketHandler):
        """Main receive loop — dispatch packets to handler."""
        while self.connected:
            packet_id, data = await self.recv_packet()
            handler.dispatch(packet_id, data)
```

**Add `parser.py` — Incoming packet handlers:**
```python
class PacketHandler:
    """Dispatches incoming packets to handler functions."""

    def __init__(self, world: WorldState):
        self.world = world
        self._handlers: dict[int, Callable] = {
            0x1B: self._login_confirm,
            0x55: self._login_complete,
            0x20: self._update_player,
            0x21: self._deny_walk,
            0x22: self._confirm_walk,
            0x77: self._update_character,
            0x78: self._update_object,
            0x1D: self._delete_object,
            0x1A: self._update_item,
            0x1C: self._talk,
            0xAE: self._unicode_talk,
            0xA1: self._update_hitpoints,
            0xA2: self._update_mana,
            0xA3: self._update_stamina,
            0x73: self._ping,
            # ... more handlers
        }

    def dispatch(self, packet_id: int, data: bytes):
        handler = self._handlers.get(packet_id)
        if handler:
            reader = PacketReader(data)
            handler(reader)
        else:
            logger.debug(f"Unhandled packet 0x{packet_id:02X}")
```

**Add basic `world_state.py`:**
```python
@dataclass
class WorldState:
    """Minimal world state for Phase 0."""
    player_serial: int = 0
    player_x: int = 0
    player_y: int = 0
    player_z: int = 0
    player_direction: int = 0
    mobiles: dict[int, MobileInfo] = field(default_factory=dict)
    items: dict[int, ItemInfo] = field(default_factory=dict)
```

**Add basic movement to `main.py`:**
```python
async def wander(conn: UoConnection, walker: WalkerManager):
    """Walk in random directions."""
    while True:
        direction = random.randint(0, 7)
        seq = walker.next_sequence()
        await conn.send_packet(0x02, build_walk_request(direction, seq, 0))
        await asyncio.sleep(0.4)  # 400ms walk delay
```

---

### Phase 1: Full Perception (1 week)

**Extend world state tracking:**

```
anima/perception/
├── world_state.py    # MobileInfo, ItemInfo, WorldState with full entity tracking
├── self_state.py     # PlayerState: stats, skills, equipment, inventory
├── social_state.py   # SpeechLog, relationships
└── event_stream.py   # GameEvent types, event queue
```

**Key additions:**
- Track all nearby mobiles (0x77, 0x78, 0xD2, 0xD3) with full stats
- Track all nearby items (0x1A, 0xF3) with properties
- Track equipment (0x2E) on mobiles
- Parse full character status (0x11) — stats, resistances, weight
- Parse skills (0x3A) — all skill values and locks
- Track speech events (0x1C, 0xAE) in journal
- Emit structured `GameEvent` objects for brain layer to consume

---

### Phase 2: Basic Brain (1 week)

```
anima/brain/
├── behavior_tree.py   # BT framework: Selector, Sequence, Action, Condition
├── goal_system.py     # Priority-based goal management
└── decision.py        # Tier routing (Phase 3)

anima/action/
├── movement.py        # WalkerManager + A* pathfinding
├── combat.py          # Attack, target, flee
├── speech.py          # Send speech packets
└── interaction.py     # DoubleClick, PickUp, Drop
```

---

## 3. Key Design Decisions

### 3.1 Async Architecture

```python
async def agent_loop(conn, world, brain):
    """Main agent loop — mirrors ClassicUO's Update()."""
    recv_task = asyncio.create_task(conn.recv_loop(world))

    while conn.connected:
        # Brain tick (100ms)
        action = brain.tick(world)
        if action:
            await action.execute(conn)
        await asyncio.sleep(0.1)
```

Two concurrent tasks:
1. **Recv loop**: continuously read packets, update world state
2. **Brain loop**: every 100ms, evaluate behavior tree, produce actions

### 3.2 Packet Parsing Pattern

ClassicUO uses handler function lookup table — we do the same:

```python
# Registration
handlers[0x78] = self._handle_update_object

# Dispatch
def dispatch(self, packet_id, data):
    if handler := self.handlers.get(packet_id):
        handler(PacketReader(data))
```

### 3.3 World State as Single Source of Truth

- All packet handlers update `WorldState` in-place
- Brain layer reads `WorldState` (never parses packets directly)
- Clean separation: `client/` produces events → `perception/` maintains state → `brain/` reads state

### 3.4 Movement Design

Port ClassicUO's `WalkerManager` pattern:
- Max 5 pending steps
- Sequence number 1-255 (wrap, never 0)
- Confirm/Deny handling with position correction
- Movement throttle (400ms walk, 200ms run)

### 3.5 Huffman Decompression

Port the static table from servuo-rs `compression.rs`. The table is 257 entries — straightforward to port to Python.

---

## 4. Dependencies

```toml
[project]
requires-python = ">=3.12"
dependencies = [
    "structlog",           # structured logging
    "pyyaml",              # persona definitions
    "pydantic>=2.0",       # data models, config validation
    "aiosqlite",           # async SQLite (Phase 4)
    "httpx",               # HTTP client for Ollama (Phase 3)
    "numpy",               # cosine similarity (Phase 4)
]

[project.optional-dependencies]
dev = [
    "pytest",
    "pytest-asyncio",
]
```

No external dependencies needed for networking (asyncio + struct are stdlib).

---

## 5. Testing Strategy

### Unit Tests
- `test_codec.py` — PacketWriter/Reader, Huffman compress/decompress
- `test_packets.py` — build/parse each packet type with known byte sequences
- `test_world_state.py` — entity create/update/delete

### Integration Tests
- `test_login.py` — connect to running servuo-rs, complete login flow
- `test_movement.py` — login, walk, verify confirm/deny responses
- `test_speech.py` — login, send speech, verify echo

### Test Data
- Capture packet dumps from ClassicUO sessions for replay testing
- Use servuo-rs test client binary dumps as reference
