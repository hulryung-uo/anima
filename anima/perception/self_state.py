"""Self state: player's own stats, skills, and equipment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from anima.perception.enums import Lock, MobileFlags, NotorietyFlag
from anima.perception.world_state import _GHOST_BODIES

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


@dataclass
class ContextMenuEntry:
    """A single entry from a context menu (0xBF sub 0x14)."""

    cliloc: int
    index: int
    flags: int


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

        # Status flags (synced from self-branch of 0x78 / 0x20)
        self.flags: MobileFlags = MobileFlags.NONE

        # Our own notoriety (criminal/gray/murderer status), synced from the
        # notoriety byte the server appends to every 0x22 ConfirmWalk. Mirrors
        # ClassicUO's world.Player.NotorietyFlag. Defaults to INNOCENT until the
        # first accepted step refreshes it.
        self.notoriety: NotorietyFlag = NotorietyFlag.INNOCENT

        # Vitals
        self.hits: int = 0
        self.hits_max: int = 0
        self.mana: int = 0
        self.mana_max: int = 0
        self.stam: int = 0
        self.stam_max: int = 0

        # Health-bar status flags (synced from 0x17 HealthBarStatusUpdate)
        self.is_poisoned: bool = False
        # Yellow / blessed health bar (0x17 status_type 2). True while the
        # server reports us blessed or yellow-barred. Mirrors MobileInfo so
        # self and foes share one field name for the dynamic invulnerable flag.
        self.is_yellow_health: bool = False
        # 0-based ServUO Poison.Level (0=Lesser .. 4=Lethal; special poisons
        # report higher raw values). -1 = not poisoned. Tracks the wire flag
        # minus one so the cure trigger can scale to severity.
        self.poison_level: int = -1

        # Stats
        self.strength: int = 0
        self.dexterity: int = 0
        self.intelligence: int = 0

        # Body attributes (from 0x11 extended status)
        # is_female: gender bool written by the server right after the type byte.
        # race: ML+ race id as on the wire (RaceID + 1): 1=Human, 2=Elf, 3=Gargoyle.
        self.is_female: bool = False
        self.race: int = 1

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

        # Context menu state (populated by 0xBF sub 0x14)
        self.context_menu_serial: int = 0
        self.context_menu: list[ContextMenuEntry] = []

    def set_body(self, body: int) -> None:
        """Set our body graphic, clearing transient poison state on death.

        ``is_poisoned`` / ``poison_level`` are a LIVING-mobile concept that
        only ever arrive on the wire via the 0x17 NewHealthbarUpdate poison
        entry (handle_health_bar_status). The poison-cured signal, however, is
        NOT reliably re-sent for our own character around death/resurrect:
        ServUO cures poison in ``Mobile.OnDeath`` (Poison = null), but the
        body flip to a ghost (0x0192/0x0193 on foot, the mounted/flying ghost
        bodies) — synced here from the 0x78/0x77/0x20 self-branches — is the
        authoritative self-death oracle (see ``is_ghost``/``is_alive``), and a
        ghost is never carried in the poison-bar stream. Without clearing the
        flag on the ghost transition, a self that was poisoned at the moment of
        death stays ``is_poisoned == True`` straight through the ghost period
        and out the other side of a resurrect (which re-flips the body via a
        plain 0x78/0x20 carrying no 0x17), so the planner's cure-priority gate
        and combat_loop's poison branch keep firing a bandage/cure against a
        phantom poison the freshly-resurrected, full-HP agent no longer has —
        starving real actions. Mirror the way ``is_yellow_health`` is already
        re-derived on every self body update: clear the stale poison the
        instant we read a ghost body. (A living->living body change leaves the
        poison alone — only the genuine death transition resets it, and a real
        re-poison after resurrect arrives on its own fresh 0x17.)

        The same death transition must also drop the cached *open-container* and
        *context-menu* UI pointers. ``open_container`` (set by 0x24
        ContainerDisplay) and ``context_menu_serial`` (0xBF sub 0x14) are only
        ever cleared by a matching 0x1D Delete for that exact serial
        (handle_delete). But death does NOT reliably delete the container the UI
        was on: ServUO closes every open gump/container in the death sequence
        without destroying the persistent entity behind it (the bank box stays
        live server-side; a looted corpse keeps its serial), so no 0x1D arrives
        for it. The pointer then survives the ghost period and out the other
        side of a resurrect. ``_wait_for_bank_box`` (skills/trade/banking.py)
        treats *any* non-zero ``open_container != backpack`` as "the bank is
        open" the instant it polls — so a freshly-resurrected agent that walks
        to a banker reads the pre-death bank serial and immediately reports the
        bank ready, then drag-drops deposits into a container the server has
        already closed: every deposit silently fails. Clear both on the genuine
        death transition, mirroring the poison clear above and the per-serial
        clears in handle_delete. (A living->living body change leaves them
        alone; a real re-open after resurrect arrives on its own fresh 0x24.)

        The cached *vendor* trading state is the third casualty of the same
        no-Delete-on-death problem. ``vendor_serial`` / ``vendor_buy_list`` /
        ``vendor_sell_list`` (set by the 0x74 / 0x9E handlers) are only ever
        cleared by a matching 0x1D Delete for the vendor (handle_delete) or an
        explicit 0xBF close (handle_close_vendor). But the vendor mobile stays
        live server-side across our death, so no 0x1D arrives, and the death
        sequence is not a vendor *close* either — so a vendor list we had open
        at the moment of death survives the ghost period and the resurrect.
        ``_wait_for_buy_list`` / ``_wait_for_sell_list`` (skills/trade/vendor.py)
        poll a *non-empty* list as the "this vendor's list is ready" signal, so a
        freshly-resurrected agent re-opening trade reads the pre-death list as
        ready the instant it polls and fires build_buy_items/build_sell_items at
        the stale ``vendor_serial`` — a doomed transaction against a vendor whose
        gump the death sequence already closed. Clear all three on death too.
        """
        was_ghost = self.body in _GHOST_BODIES
        self.body = body
        if body in _GHOST_BODIES and not was_ghost:
            self.is_poisoned = False
            self.poison_level = -1
            self.open_container = 0
            self.context_menu_serial = 0
            self.context_menu = []
            self.vendor_serial = 0
            self.vendor_buy_list = []
            self.vendor_sell_list = []

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
    def is_ghost(self) -> bool:
        """True while our own mobile is wearing a ghost body graphic.

        On death ServUO turns the player into a ghost (0x0192/0x0193 on foot,
        or the mounted/flying ghost bodies) and that body change — synced by
        the 0x78/0x20/0x77 handlers into ``self.body`` — is the authoritative
        death signal ClassicUO uses (Mobile.IsDead). The health-bar packets
        (0x11/0xA1) are NOT a reliable death oracle for self: a ghost can show
        a non-zero bar, and an out-of-order 0xA1 can land after the body flip
        still carrying the pre-death ``hits``. Mirrors WorldState._GHOST_BODIES
        so self and foes share one corpse criterion.
        """
        return self.body in _GHOST_BODIES

    @property
    def is_alive(self) -> bool:
        # A ghost body is dead no matter what the health bar says — the body
        # flip is the authoritative death signal (see is_ghost). Otherwise we
        # are alive if we have positive HP, or if HP is simply unknown
        # (hits_max==0 placeholder before the first status packet arrives).
        if self.is_ghost:
            return False
        return self.hits > 0 or self.hits_max == 0

    @property
    def hidden(self) -> bool:
        """True while the server reports us hidden (UO flag 0x80)."""
        return bool(self.flags & MobileFlags.HIDDEN)

    @property
    def in_war_mode(self) -> bool:
        """True while the server reports us in war mode (UO flag 0x40)."""
        return bool(self.flags & MobileFlags.WAR_MODE)
