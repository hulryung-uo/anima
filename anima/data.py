"""UO static data lookups — item names from tiledata, cliloc strings."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"


@lru_cache(maxsize=1)
def _load_tiledata() -> dict[str, dict]:
    path = DATA_DIR / "tiledata_items.json"
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


@lru_cache(maxsize=1)
def _load_cliloc() -> dict[str, str]:
    path = DATA_DIR / "cliloc.json"
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def item_name(graphic: int) -> str:
    """Look up item name by graphic ID. Returns '' if not found."""
    data = _load_tiledata()
    entry = data.get(str(graphic))
    if entry:
        name = entry["name"]
        # Strip UO format codes like %s%
        return name.replace("%s%", "").replace("%", "").strip()
    return ""


def cliloc_text(number: int) -> str:
    """Look up cliloc string by number. Returns '' if not found."""
    data = _load_cliloc()
    return data.get(str(number), "")


# Common body IDs → human-readable names
_BODY_NAMES: dict[int, str] = {
    0x0190: "Human Male",
    0x0191: "Human Female",
    0x0192: "Ghost Male",
    0x0193: "Ghost Female",
    0x00C8: "Horse",
    0x00E2: "Horse",
    0x00CC: "Horse",
    0x00C9: "Cat",
    0x00D9: "Dog",
    0x00CB: "Goat",
    0x00D7: "Deer",
    0x00D3: "Bird",
    0x00D0: "Chicken",
    0x00DC: "Llama",
    0x00E1: "Wolf",
    0x00E8: "Cow",
    0x00EA: "Bull",
    0x00EE: "Rat",
    0x000D: "Orc",
    0x0001: "Ogre",
    0x0002: "Ettin",
    0x0003: "Zombie",
    0x0004: "Gargoyle",
    0x0005: "Eagle",
    0x0006: "Bird",
    0x0009: "Daemon",
    0x000A: "Dragon",
    0x000E: "Troll",
    0x0015: "Giant Spider",
    0x0023: "Lich",
    0x0024: "Skeleton",
    0x003A: "Wisp",
    0x0039: "Slime",
}


def body_name(body_id: int) -> str:
    """Look up mobile body name by ID. Returns hex string if not found."""
    return _BODY_NAMES.get(body_id, f"0x{body_id:04X}")


def mobile_display_name(mob: object) -> str:
    """Best human-readable name for a mobile.

    Priority: mob.name > OPL properties > body_name lookup.
    """
    name = getattr(mob, "name", "") or ""
    if name:
        return name

    # OPL properties: [0]=name, [1]=title, etc.
    props = getattr(mob, "properties", None) or []
    if props:
        # Combine name + title if both exist
        parts = [p for p in props[:2] if p]
        if parts:
            return " ".join(parts)

    body_id = getattr(mob, "body", 0)
    return body_name(body_id) if body_id else "?"
