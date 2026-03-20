"""Self state: player's own stats, skills, and equipment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from anima.perception.enums import Lock

if TYPE_CHECKING:
    from anima.perception.gump import GumpData


@dataclass
class SkillInfo:
    id: int
    value: float = 0.0  # current value (tenths)
    base: float = 0.0  # base value (tenths)
    cap: float = 0.0  # skill cap (tenths)
    lock: Lock = Lock.UP


@dataclass
class VendorBuyItem:
    """An item available for purchase from a vendor (from 0x74 + 0x3C)."""

    serial: int
    graphic: int
    amount: int
    price: int
    name: str


@dataclass
class VendorSellItem:
    """A player item that a vendor will buy (from 0x9E)."""

    serial: int
    graphic: int
    amount: int
    price: int
    name: str


class SelfState:
    """Player's own character state — stats, skills, equipment."""

    def __init__(self, serial: int = 0) -> None:
        self.serial: int = serial
        self.name: str = ""
        self.body: int = 0

        # Position (synced by WalkerManager)
        self.x: int = 0
        self.y: int = 0
        self.z: int = 0
        self.direction: int = 0

        # Vitals
        self.hits: int = 0
        self.hits_max: int = 0
        self.mana: int = 0
        self.mana_max: int = 0
        self.stam: int = 0
        self.stam_max: int = 0

        # Stats
        self.strength: int = 0
        self.dexterity: int = 0
        self.intelligence: int = 0

        # Extended stats (from 0x11)
        self.gold: int = 0
        self.weight: int = 0
        self.weight_max: int = 0
        self.armor: int = 0
        self.damage_min: int = 0
        self.damage_max: int = 0
        self.luck: int = 0
        self.stat_cap: int = 0
        self.followers: int = 0
        self.followers_max: int = 0
        self.resist_fire: int = 0
        self.resist_cold: int = 0
        self.resist_poison: int = 0
        self.resist_energy: int = 0

        # Skills
        self.skills: dict[int, SkillInfo] = {}

        # Equipment serials by layer
        self.equipment: dict[int, int] = {}  # layer -> item serial

        # Pending target cursor from server (set by 0x6C handler, consumed by skills)
        self.pending_target: dict | None = None

        # Active gumps from server, keyed by gump_id
        self.gumps: dict[int, GumpData] = {}

        # Combat state
        self.last_damage_taken_at: float = 0.0  # time.monotonic() of last hit

        # Container state
        self.open_container: int = 0  # serial of last opened container (0x24)

        # Vendor trading state (populated by 0x74 / 0x9E handlers)
        self.vendor_serial: int = 0
        self.vendor_buy_list: list[VendorBuyItem] = []
        self.vendor_sell_list: list[VendorSellItem] = []

    @property
    def hp_percent(self) -> float:
        if self.hits_max == 0:
            return 100.0
        return (self.hits / self.hits_max) * 100.0

    @property
    def mana_percent(self) -> float:
        if self.mana_max == 0:
            return 100.0
        return (self.mana / self.mana_max) * 100.0

    @property
    def stam_percent(self) -> float:
        if self.stam_max == 0:
            return 100.0
        return (self.stam / self.stam_max) * 100.0

    @property
    def is_alive(self) -> bool:
        return self.hits > 0 or self.hits_max == 0
