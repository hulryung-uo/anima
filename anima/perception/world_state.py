"""World state: tracking of mobiles and items in the game world."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from anima.perception.enums import Direction, MobileFlags, NotorietyFlag


# Ghost body graphics, exactly mirroring ClassicUO's Mobile.IsDead
# (Game/GameObjects/Mobile.cs): the two on-foot human ghosts PLUS the
# mounted/flying ghost bodies. A mobile that dies while mounted or flying
# turns into 0x025F-0x0260 / 0x02B6 / 0x02B7 — never 0x0192/0x0193 — so a
# check limited to the on-foot pair silently fails to flag a mounted enemy's
# corpse as dead. That corpse then keeps counting as a live hostile in the
# swarm tally, in _find_target focus-fire, and in heal-target selection.
_GHOST_BODIES: frozenset[int] = frozenset(
    {0x0192, 0x0193, 0x02B6, 0x02B7} | set(range(0x025F, 0x0261))
)


@dataclass
class MobileInfo:
    serial: int
    x: int = 0
    y: int = 0
    z: int = 0
    direction: Direction = Direction.NORTH
    body: int = 0
    hue: int = 0
    flags: MobileFlags = MobileFlags.NONE
    notoriety: NotorietyFlag = NotorietyFlag.INNOCENT
    name: str = ""
    hits_max: int = 0
    hits: int = 0
    is_poisoned: bool = False
    # Yellow / blessed health bar (ServUO 0x17 status_type 2 = m.Blessed ||
    # m.YellowHealthbar). The dynamic invulnerable signal — ClassicUO's
    # Mobile.IsYellowHits. Distinct from the static INVULNERABLE notoriety.
    is_yellow_health: bool = False
    # 0-based ServUO Poison.Level (0=Lesser .. 4=Lethal). -1 = not poisoned.
    poison_level: int = -1
    # time.monotonic() of the last packet that touched this mobile. The UO
    # server does NOT reliably send 0x1D Delete for every mobile that leaves
    # view (notably after recall/teleport, or when many entities go out of
    # range at once), so without an age stamp stale phantoms linger forever.
    last_seen: float = 0.0
    properties: list[str] = field(default_factory=list)  # OPL tooltip lines

    @property
    def is_dead(self) -> bool:
        # A mobile is dead if it has turned into a ghost body, OR if a KNOWN
        # health bar reads zero. The latter is the exact criterion the combat
        # loop uses for "this target is dead" (combat_loop.py): hits_max>0 and
        # hits<=0. Mobiles we have never queried default hits/hits_max to 0 —
        # those must NOT count as dead (hits_max==0 guard), otherwise every
        # un-inspected mob would be treated as a corpse.
        # _GHOST_BODIES covers the on-foot AND mounted/flying ghost graphics,
        # matching ClassicUO Mobile.IsDead.
        if self.body in _GHOST_BODIES:
            return True
        return self.hits_max > 0 and self.hits <= 0


@dataclass
class ItemInfo:
    serial: int
    x: int = 0
    y: int = 0
    z: int = 0
    graphic: int = 0
    hue: int = 0
    amount: int = 1
    container: int = 0  # 0 = on ground, else parent serial
    layer: int = 0
    name: str = ""
    # True when the server tagged this as a BaseMulti (0x1A graphic bit 0x4000
    # / 0xF3 data_type 0x02). Multis are house/addon components, not pickable
    # items — callers filter them out of nearest-item / loot lookups.
    is_multi: bool = False
    properties: list[str] = field(default_factory=list)  # OPL tooltip lines


class WorldState:
    """Tracks all mobiles and items visible in the game world."""

    def __init__(self) -> None:
        self.mobiles: dict[int, MobileInfo] = {}
        self.items: dict[int, ItemInfo] = {}
        self.opl_revisions: dict[int, int] = {}  # serial → revision hash
        # OPL name/property cache that survives 0x1D Delete. When an NPC
        # leaves view and re-enters, its MobileInfo is recreated blank by
        # 0x78 MobileIncoming; without this cache the name would be "" until
        # the next OPL round-trip completes, which races every synchronous
        # vendor lookup (_is_vendor / _is_refused).
        #
        # The same divergence hits ITEMS: 0xD6 MegaCliloc caches name/properties
        # by serial for items too, so get_or_create_item restores from it as well.
        self.opl_names: dict[int, str] = {}
        self.opl_properties: dict[int, list[str]] = {}

    def get_or_create_mobile(self, serial: int) -> MobileInfo:
        if serial not in self.mobiles:
            mob = MobileInfo(serial=serial)
            cached_name = self.opl_names.get(serial)
            if cached_name:
                mob.name = cached_name
            cached_props = self.opl_properties.get(serial)
            if cached_props:
                mob.properties = list(cached_props)
            self.mobiles[serial] = mob
        mob = self.mobiles[serial]
        # Every handler that updates a mobile (0x77/0x78/0x20/0x17/0xA1/OPL)
        # routes through here, so stamping last_seen on each touch keeps the
        # freshness clock current with no per-handler changes.
        mob.last_seen = time.monotonic()
        return mob

    def touch_existing_mobile(self, serial: int) -> MobileInfo | None:
        """Refresh ``last_seen`` on a mobile that already exists, else None.

        The phantom-guard handlers (non-self 0xA1 UpdateCurrentHealth, 0x2D
        MobileAttributes, 0x11 CharacterStatus) must NEVER create a mobile —
        those packets carry no position, and a serial we no longer have in view
        would spawn a positionless corpse (see the per-handler comments). They
        therefore stopped routing through ``get_or_create_mobile`` and instead
        do ``self.mobiles.get(serial)``, which silently dropped the ``last_seen``
        stamp that every other touch goes through.

        That matters during a STATIONARY melee fight: a foe that stays in range
        but issues no 0x77 MobileMoving is refreshed ONLY by the streaming 0xA1
        health updates. Without re-stamping ``last_seen`` here, those updates
        leave the freshness clock frozen, so ``prune_stale_mobiles`` would reap
        the foe out of ``world.mobiles`` mid-combat (breaking target acquisition
        / focus-fire / the swarm tally) and the next 0xA1 — held back by the
        very phantom guard — could not re-create it. Mirror ClassicUO: only the
        spatial packets create a mobile, but ANY packet that updates one keeps it
        fresh. Returns the existing mobile (with ``last_seen`` bumped) or None.
        """
        mob = self.mobiles.get(serial)
        if mob is None:
            return None
        mob.last_seen = time.monotonic()
        return mob

    def get_or_create_item(self, serial: int) -> ItemInfo:
        if serial not in self.items:
            item = ItemInfo(serial=serial)
            # Restore the cached OPL name/properties, exactly like
            # get_or_create_mobile does for mobiles. 0xD6 MegaCliloc is the
            # ONLY item-name source and is requested/pushed independently of the
            # item's spatial packets (0x1A WorldItem / 0x3C ContainerContent /
            # 0x25 AddItemToContainer). So an OPL can arrive BEFORE the item
            # exists, or the item can leave view (0x1D Delete) and re-enter —
            # either way the spatial packet recreates a BLANK ItemInfo. Without
            # this restore the item's name/properties stay "" until a fresh OPL
            # round-trip completes, racing every name-keyed item lookup (loot
            # filtering, reagent/tool identification, the vendor buy-list name
            # correlation). The cache (populated by handle_mega_cliloc) already
            # survives remove(); mobiles consumed it, items silently did not.
            cached_name = self.opl_names.get(serial)
            if cached_name:
                item.name = cached_name
            cached_props = self.opl_properties.get(serial)
            if cached_props:
                item.properties = list(cached_props)
            self.items[serial] = item
        return self.items[serial]

    def remove(self, serial: int) -> None:
        # Removing a container (a corpse despawning, a dropped bag destroyed,
        # a backpack on a vanishing mobile) must also drop everything that
        # lived INSIDE it. ClassicUO's World.RemoveItem / RemoveMobile walk
        # the entity's child list and recursively RemoveItem each one
        # (Game/World.cs). We don't keep a forward child list, so we scan
        # world.items for anything whose `container` points at the serial
        # being removed and recurse into those (a child may itself be a
        # nested container, e.g. a bag inside a corpse).
        #
        # Without this, orphaned children linger forever with a dangling
        # `container` pointer: they pollute nearby_items / container-content
        # lookups, corrupt the 0x74 vendor buy-list correlation (which
        # collects items by `it.container == container_serial`), and leak
        # memory across a long eval run.
        children = [
            child for child, it in self.items.items() if it.container == serial
        ]
        for child in children:
            self.remove(child)
        self.mobiles.pop(serial, None)
        self.items.pop(serial, None)
        # Intentionally keep opl_names/opl_properties — see
        # get_or_create_mobile.

    def prune_stale_mobiles(
        self, now: float | None = None, max_age: float = 30.0
    ) -> list[int]:
        """Drop mobiles the server has stopped updating.

        A mobile whose ``last_seen`` is older than ``max_age`` seconds is
        treated as despawned-without-Delete and removed, so ``nearby_mobiles``
        never returns phantoms parked at stale coordinates (e.g. after the
        player recalls/teleports and the server omits the 0x1D Delete).

        Returns the list of pruned serials. ``last_seen == 0.0`` (never
        stamped) is skipped so freshly seeded test fixtures aren't reaped.
        """
        if now is None:
            now = time.monotonic()
        stale = [
            serial
            for serial, m in self.mobiles.items()
            if m.last_seen > 0.0 and (now - m.last_seen) > max_age
        ]
        for serial in stale:
            # Route through remove() rather than popping the mobile directly:
            # a mobile's worn items and backpack contents live in world.items
            # with `container == serial` (set by 0x78 MobileIncoming). Popping
            # only self.mobiles strands those children with a dangling
            # `container` pointer — the very leak remove() was written to
            # prevent (polluting nearby_items / vendor-list correlation and
            # growing unboundedly across a long eval). remove() cascade-deletes
            # the whole subtree, matching ClassicUO World.RemoveMobile.
            self.remove(serial)
        return stale

    def nearby_mobiles(self, x: int, y: int, distance: int = 18) -> list[MobileInfo]:
        result = []
        for m in self.mobiles.values():
            if abs(m.x - x) <= distance and abs(m.y - y) <= distance:
                result.append(m)
        return result

    def nearby_items(self, x: int, y: int, distance: int = 18) -> list[ItemInfo]:
        result = []
        for item in self.items.values():
            if item.container == 0 and abs(item.x - x) <= distance and abs(item.y - y) <= distance:
                result.append(item)
        return result
