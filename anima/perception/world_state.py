"""World state: tracking of mobiles and items in the game world."""

from __future__ import annotations

from dataclasses import dataclass

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

    @property
    def is_dead(self) -> bool:
        return self.body in (0x0192, 0x0193)  # ghost bodies


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


class WorldState:
    """Tracks all mobiles and items visible in the game world."""

    def __init__(self) -> None:
        self.mobiles: dict[int, MobileInfo] = {}
        self.items: dict[int, ItemInfo] = {}

    def get_or_create_mobile(self, serial: int) -> MobileInfo:
        if serial not in self.mobiles:
            self.mobiles[serial] = MobileInfo(serial=serial)
        return self.mobiles[serial]

    def get_or_create_item(self, serial: int) -> ItemInfo:
        if serial not in self.items:
            self.items[serial] = ItemInfo(serial=serial)
        return self.items[serial]

    def remove(self, serial: int) -> None:
        self.mobiles.pop(serial, None)
        self.items.pop(serial, None)

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
