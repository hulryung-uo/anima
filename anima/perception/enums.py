"""UO protocol enums used across the perception layer."""

from __future__ import annotations

from enum import IntEnum, IntFlag


class Direction(IntEnum):
    NORTH = 0
    RIGHT = 1
    EAST = 2
    DOWN = 3
    SOUTH = 4
    LEFT = 5
    WEST = 6
    UP = 7

    RUNNING = 0x80  # OR'd with direction

    @staticmethod
    def from_byte(b: int) -> Direction:
        return Direction(b & 0x07)


class NotorietyFlag(IntEnum):
    INNOCENT = 1  # blue
    ALLY = 2  # green
    ATTACKABLE = 3  # gray (can be attacked)
    CRIMINAL = 4  # gray (criminal)
    ENEMY = 5  # orange
    MURDERER = 6  # red
    INVULNERABLE = 7  # yellow


class MobileFlags(IntFlag):
    NONE = 0x00
    FROZEN = 0x01
    FEMALE = 0x02
    FLYING = 0x04
    BLESSED = 0x08
    WAR_MODE = 0x40
    HIDDEN = 0x80


class Layer(IntEnum):
    INVALID = 0x00
    ONE_HANDED = 0x01
    TWO_HANDED = 0x02
    SHOES = 0x03
    PANTS = 0x04
    SHIRT = 0x05
    HELM = 0x06
    GLOVES = 0x07
    RING = 0x08
    TALISMAN = 0x09
    NECK = 0x0A
    HAIR = 0x0B
    WAIST = 0x0C
    INNER_TORSO = 0x0D
    BRACELET = 0x0E
    FACE = 0x0F
    FACIAL_HAIR = 0x10
    MIDDLE_TORSO = 0x11
    EARRINGS = 0x12
    ARMS = 0x13
    CLOAK = 0x14
    BACKPACK = 0x15
    OUTER_TORSO = 0x16
    OUTER_LEGS = 0x17
    INNER_LEGS = 0x18
    MOUNT = 0x19
    SHOP_BUY = 0x1A
    SHOP_RESALE = 0x1B
    SHOP_SELL = 0x1C
    BANK = 0x1D


class Lock(IntEnum):
    UP = 0
    DOWN = 1
    LOCKED = 2


class MessageType(IntEnum):
    REGULAR = 0x00
    SYSTEM = 0x01
    EMOTE = 0x02
    LABEL = 0x06
    FOCUS = 0x07
    WHISPER = 0x08
    YELL = 0x09
    SPELL = 0x0A
    GUILD = 0x0D
    ALLIANCE = 0x0E
    PARTY = 0x0F
