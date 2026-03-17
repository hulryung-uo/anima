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
