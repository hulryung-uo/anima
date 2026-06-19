"""World state: tracking of mobiles and items in the game world."""

from __future__ import annotations

from dataclasses import dataclass, field

from anima.perception.enums import Direction, MobileFlags, NotorietyFlag


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
    properties: list[str] = field(default_factory=list)  # OPL tooltip lines

    @property
    def is_dead(self) -> bool:
        # A mobile is dead if it has turned into a ghost body, OR if a KNOWN
        # health bar reads zero. The latter is the exact criterion the combat
        # loop uses for "this target is dead" (combat_loop.py): hits_max>0 and
        # hits<=0. Mobiles we have never queried default hits/hits_max to 0 —
        # those must NOT count as dead (hits_max==0 guard), otherwise every
        # un-inspected mob would be treated as a corpse.
        if self.body in (0x0192, 0x0193):  # ghost bodies
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
        return self.mobiles[serial]

    def get_or_create_item(self, serial: int) -> ItemInfo:
        if serial not in self.items:
            self.items[serial] = ItemInfo(serial=serial)
        return self.items[serial]

    def remove(self, serial: int) -> None:
        self.mobiles.pop(serial, None)
        self.items.pop(serial, None)
        # Intentionally keep opl_names/opl_properties — see
        # get_or_create_mobile.

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
