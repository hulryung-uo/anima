"""MockWorld — minimal world simulator for planner temporal tests.

Provides a deterministic, in-process substitute for the live UO world so that
multi-tick planner sequences can be exercised without network I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MockOreBank:
    """A mineable ore location with a finite supply that depletes and respawns."""

    x: int
    y: int
    ore_count: int  # remaining ore; 0 = depleted
    respawn_at: float = 0.0  # simulation time when it refills

    def try_mine(self, amount: int = 1) -> int:
        """Consume up to *amount* ore. Returns how much was actually mined."""
        mined = min(amount, self.ore_count)
        self.ore_count -= mined
        return mined


@dataclass
class MockItem:
    """A single item that can exist on the ground or in a container."""

    serial: int
    graphic: int
    amount: int = 1
    hue: int = 0
    x: int = 0
    y: int = 0
    container: int = 0  # 0 = ground, else backpack serial
    name: str = ""


# ---------------------------------------------------------------------------
# WorldBehavior — strategy pattern so tests can customise outcomes
# ---------------------------------------------------------------------------

class WorldBehavior:
    """Default behavior: most procedures succeed; mines deplete naturally.

    Subclass and override *on_procedure_selected* to inject failures or
    custom outcomes for specific scenarios.
    """

    def on_procedure_selected(
        self,
        world: "MockWorld",
        procedure_name: str,
    ) -> tuple[bool, str]:
        """Decide the outcome of a single planner tick.

        Returns ``(success, reason_string)``.

        The default policy:
        - ``mine_ore``          → trigger a successful mine at the player
                                  position (up to 1 ore), or failure if the
                                  nearest bank is depleted.
        - ``smelt_ore``         → convert all ore in backpack to ingots.
        - ``craft_blacksmith``  → simulate a craft attempt (succeeds if
                                  8+ ingots available).
        - ``move_to_*`` / any   → success (player teleports to destination).
        """
        if procedure_name == "mine_ore":
            return self._handle_mine(world)
        if procedure_name == "smelt_ore":
            return self._handle_smelt(world)
        if procedure_name == "craft_blacksmith":
            return self._handle_craft(world)
        # Default: succeed silently
        return True, "ok"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _handle_mine(self, world: "MockWorld") -> tuple[bool, str]:
        """Mine from the nearest non-depleted bank."""
        px, py = world.player_x, world.player_y
        nearest = min(
            world.ore_banks,
            key=lambda b: abs(b.x - px) + abs(b.y - py),
            default=None,
        )
        if nearest is None:
            return False, "no_ore_bank"
        if nearest.ore_count == 0:
            world.trigger_mine_failure(nearest.x, nearest.y, reason="depleted")
            return False, "depleted"
        gained = nearest.try_mine(1)
        world.trigger_mine_success(nearest.x, nearest.y, amount=gained)
        return True, "mined"

    def _handle_smelt(self, world: "MockWorld") -> tuple[bool, str]:
        """Convert ore items in backpack into ingots (1:1 ratio)."""
        from anima.skills.gathering.mine import ORE_GRAPHICS  # lazy import

        backpack = world._backpack_serial
        ore_items = [
            it for it in world._items.values()
            if it.container == backpack and it.graphic in ORE_GRAPHICS
        ]
        total_ore = sum(it.amount for it in ore_items)
        if total_ore == 0:
            return False, "no_ore"
        # Remove ore items
        for it in ore_items:
            del world._items[it.serial]
        # Add ingots
        from anima.skills.crafting.smelt import INGOT_GRAPHICS
        ingot_graphic = next(iter(INGOT_GRAPHICS))
        ingot_serial = world._next_serial()
        ingot = MockItem(
            serial=ingot_serial,
            graphic=ingot_graphic,
            amount=total_ore,
            container=backpack,
            name="iron ingot",
        )
        world._items[ingot_serial] = ingot
        return True, f"smelted_{total_ore}"

    def _handle_craft(self, world: "MockWorld") -> tuple[bool, str]:
        """Simulate blacksmith crafting (consume 8 ingots → 1 crafted item)."""
        from anima.skills.crafting.smelt import INGOT_GRAPHICS

        backpack = world._backpack_serial
        ingot_items = [
            it for it in world._items.values()
            if it.container == backpack and it.graphic in INGOT_GRAPHICS and it.hue == 0
        ]
        total = sum(it.amount for it in ingot_items)
        if total < 8:
            return False, "insufficient_metal"
        # Consume 8 ingots
        remaining = 8
        for it in ingot_items:
            if remaining <= 0:
                break
            take = min(it.amount, remaining)
            it.amount -= take
            remaining -= take
            if it.amount == 0:
                del world._items[it.serial]
        # Add a crafted item (dagger graphic 0x0F51)
        crafted_serial = world._next_serial()
        crafted = MockItem(
            serial=crafted_serial,
            graphic=0x0F51,
            amount=1,
            container=backpack,
            name="dagger",
        )
        world._items[crafted_serial] = crafted
        return True, "crafted_item"


# ---------------------------------------------------------------------------
# MockWorld
# ---------------------------------------------------------------------------

_BACKPACK_LAYER = 0x15
_BACKPACK_SERIAL = 0x101


class MockWorld:
    """Minimal world simulator for planner temporal tests.

    Holds:
    - Player position (x, y, z)
    - Player inventory (MockItem dict keyed by serial)
    - Ore banks (list of MockOreBank)
    - Simulation time (increments each tick)
    - Event log (for assertions)

    Exposes an AgentContext-compatible interface via ``.as_ctx()`` so the
    planner can consume it without a real server connection.
    """

    def __init__(self, start_x: int = 100, start_y: int = 100) -> None:
        self.player_x: int = start_x
        self.player_y: int = start_y
        self.player_z: int = 0

        # Stats
        self._gold: int = 50
        self._hits: int = 100
        self._hits_max: int = 100
        self._weight: int = 100
        self._weight_max: int = 400

        # Items keyed by serial
        self._items: dict[int, MockItem] = {}
        self._serial_counter: int = 0x200

        # Backpack is pre-created (layer 0x15, serial 0x101)
        self._backpack_serial: int = _BACKPACK_SERIAL

        # Ore banks
        self.ore_banks: list[MockOreBank] = []

        # Simulation clock
        self._time: float = 0.0

        # Event log
        self._event_log: list[dict] = []

    # ------------------------------------------------------------------
    # Time
    # ------------------------------------------------------------------

    @property
    def time(self) -> float:
        return self._time

    def advance_time(self, seconds: float) -> None:
        self._time += seconds
        # Respawn ore banks whose timer has expired
        for bank in self.ore_banks:
            if bank.ore_count == 0 and bank.respawn_at > 0 and self._time >= bank.respawn_at:
                bank.ore_count = 20
                bank.respawn_at = 0.0
                self._event_log.append({
                    "type": "respawn",
                    "x": bank.x,
                    "y": bank.y,
                    "t": self._time,
                })

    # ------------------------------------------------------------------
    # Serial generator
    # ------------------------------------------------------------------

    def _next_serial(self) -> int:
        self._serial_counter += 1
        return self._serial_counter

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def add_mine_bank(self, x: int, y: int, ore_count: int = 20) -> MockOreBank:
        """Register a mineable ore bank at the given map coordinates."""
        bank = MockOreBank(x=x, y=y, ore_count=ore_count)
        self.ore_banks.append(bank)
        return bank

    def add_item_to_backpack(
        self, graphic: int, amount: int = 1, hue: int = 0, name: str = ""
    ) -> MockItem:
        """Place an item directly into the player's backpack."""
        serial = self._next_serial()
        item = MockItem(
            serial=serial,
            graphic=graphic,
            amount=amount,
            hue=hue,
            container=self._backpack_serial,
            name=name,
        )
        self._items[serial] = item
        return item

    def set_player_stats(
        self,
        gold: int = 0,
        hits: int = 100,
        weight: int = 0,
    ) -> None:
        self._gold = gold
        self._hits = hits
        self._weight = weight

    def set_player_position(self, x: int, y: int) -> None:
        self.player_x = x
        self.player_y = y

    # ------------------------------------------------------------------
    # Simulation events
    # ------------------------------------------------------------------

    def trigger_mine_success(self, x: int, y: int, amount: int = 1) -> int:
        """Simulate a successful mine at (x, y). Adds ore to backpack.

        Returns the amount actually added.
        """
        from anima.skills.gathering.mine import ORE_GRAPHICS

        ore_graphic = next(iter(ORE_GRAPHICS))
        serial = self._next_serial()
        item = MockItem(
            serial=serial,
            graphic=ore_graphic,
            amount=amount,
            container=self._backpack_serial,
            name="ore",
            x=x,
            y=y,
        )
        self._items[serial] = item
        self._weight += amount * 2  # approximate weight
        self._event_log.append({
            "type": "mine_success",
            "x": x,
            "y": y,
            "amount": amount,
            "t": self._time,
        })
        return amount

    def trigger_mine_failure(self, x: int, y: int, reason: str = "depleted") -> None:
        """Record a mine failure event (no items added)."""
        self._event_log.append({
            "type": "mine_failure",
            "x": x,
            "y": y,
            "reason": reason,
            "t": self._time,
        })

    # ------------------------------------------------------------------
    # Query helpers for assertions
    # ------------------------------------------------------------------

    def ore_mined_at(self, x: int, y: int) -> int:
        """Total ore successfully mined at the given bank coordinates."""
        return sum(
            e["amount"]
            for e in self._event_log
            if e["type"] == "mine_success" and e["x"] == x and e["y"] == y
        )

    def procedures_selected(self) -> list[str]:
        """Return the ordered list of procedure names selected across all ticks."""
        return [
            e["procedure"]
            for e in self._event_log
            if e["type"] == "procedure_selected"
        ]

    def total_ticks_wasted_at_bank(self, x: int, y: int) -> int:
        """Number of failed mine attempts recorded at the given bank."""
        return sum(
            1
            for e in self._event_log
            if e["type"] == "mine_failure" and e["x"] == x and e["y"] == y
        )

    # ------------------------------------------------------------------
    # Internal log helper used by runner
    # ------------------------------------------------------------------

    def _record_procedure_selected(self, name: str) -> None:
        self._event_log.append({
            "type": "procedure_selected",
            "procedure": name,
            "t": self._time,
        })

    # ------------------------------------------------------------------
    # AgentContext bridge
    # ------------------------------------------------------------------

    def as_ctx(self) -> MagicMock:
        """Return an AgentContext-shaped mock the planner can consume.

        The returned mock is configured with:
        - ``.perception.self_state`` — player stats drawn from this world
        - ``.perception.world.items`` — live dict of MockItems
        - ``.perception.world.nearby_mobiles()`` — returns [] (no mobs)
        - ``.blackboard`` — shared mutable dict (persists across calls)
        - ``.conn.connected = True``
        - ``.conn.send_packet`` — AsyncMock (no-op)
        - ``.bus = None``
        - ``.persona.name = "TestAgent"``
        - ``.memory_db = None``
        - ``.llm = None``
        - ``.map_reader = None``
        """
        ctx = MagicMock()

        # ---- self_state ----
        ss = ctx.perception.self_state
        ss.x = self.player_x
        ss.y = self.player_y
        ss.z = self.player_z
        ss.hits = self._hits
        ss.hits_max = self._hits_max
        ss.weight = self._weight
        ss.weight_max = self._weight_max
        ss.gold = self._gold
        ss.serial = 0x01  # player serial
        # Backpack is registered in equipment layer 0x15
        ss.equipment = {_BACKPACK_LAYER: self._backpack_serial}

        # Skills dict — empty but present so attribute access works
        ss.skills = {}

        # ---- world.items — live reference so mutations are visible ----
        ctx.perception.world.items = self._items

        # nearby_mobiles returns empty list by default
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[])

        # ---- blackboard — persist between ticks by reusing on the ctx ----
        # We store it as an instance attribute so repeated as_ctx() calls
        # share the same blackboard dict.
        if not hasattr(self, "_blackboard"):
            self._blackboard: dict = {}
        ctx.blackboard = self._blackboard

        # ---- connection ----
        ctx.conn.connected = True
        ctx.conn.send_packet = AsyncMock()

        # ---- misc ----
        ctx.bus = None
        ctx.persona = MagicMock()
        ctx.persona.name = "TestAgent"
        ctx.memory_db = None
        ctx.llm = None
        ctx.map_reader = None

        return ctx
