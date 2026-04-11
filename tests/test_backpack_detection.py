"""Regression tests for player backpack detection at login.

Context: at login, inspect_self scans world items by backpack graphic as a
fallback when the equipment packet hasn't populated Layer.BACKPACK. The
previous buggy OR condition (`container == player OR layer == 0x15`)
would match any nearby NPC's backpack because every mobile carries a
backpack at layer 0x15 on their own body.

This test fixes the scenario where Grimm logged in inside Minoc Blacksmith
with 5 NPCs nearby, and the fallback grabbed NPC 0x00000BBE's backpack
serial 0x40014AD1 instead of the player's own 0x4001ABD1 from cache. The
agent then thought its own backpack was empty despite weight=315.
"""

from anima.main import _find_player_backpack
from anima.perception.world_state import ItemInfo


PLAYER_SERIAL = 0x00002C95
PLAYER_BACKPACK_SERIAL = 0x4001ABD1
NPC1_SERIAL = 0x00000BBE
NPC1_BACKPACK_SERIAL = 0x40014AD1

REGULAR_BACKPACK_GRAPHIC = 0x0E75


def _backpack_item(serial: int, container: int, layer: int = 0x15) -> ItemInfo:
    """Build a backpack ItemInfo for tests."""
    it = ItemInfo(serial=serial)
    it.graphic = REGULAR_BACKPACK_GRAPHIC
    it.container = container
    it.layer = layer
    return it


def test_picks_player_backpack_when_only_player_has_one():
    items = {
        PLAYER_BACKPACK_SERIAL: _backpack_item(
            PLAYER_BACKPACK_SERIAL, container=PLAYER_SERIAL,
        ),
    }
    assert _find_player_backpack(items, PLAYER_SERIAL) == PLAYER_BACKPACK_SERIAL


def test_ignores_nearby_npc_backpack():
    """Regression: must not pick an NPC's backpack just because layer==0x15."""
    items = {
        NPC1_BACKPACK_SERIAL: _backpack_item(
            NPC1_BACKPACK_SERIAL, container=NPC1_SERIAL,
        ),
    }
    assert _find_player_backpack(items, PLAYER_SERIAL) is None


def test_prefers_player_backpack_over_npc_backpacks():
    """With both present, pick the one equipped on the player."""
    items = {
        NPC1_BACKPACK_SERIAL: _backpack_item(
            NPC1_BACKPACK_SERIAL, container=NPC1_SERIAL,
        ),
        PLAYER_BACKPACK_SERIAL: _backpack_item(
            PLAYER_BACKPACK_SERIAL, container=PLAYER_SERIAL,
        ),
        0x40014AD2: _backpack_item(0x40014AD2, container=0x00002461),  # another NPC
    }
    assert _find_player_backpack(items, PLAYER_SERIAL) == PLAYER_BACKPACK_SERIAL


def test_rejects_wrong_layer_even_if_container_matches():
    """An item in player's container at a non-backpack layer is not the backpack."""
    items = {
        0x40014AD1: _backpack_item(
            0x40014AD1, container=PLAYER_SERIAL, layer=0x01,  # e.g. one-handed
        ),
    }
    assert _find_player_backpack(items, PLAYER_SERIAL) is None


def test_rejects_non_backpack_graphic():
    """An item at container+layer match but wrong graphic is not the backpack."""
    it = ItemInfo(serial=0x40014AD1)
    it.graphic = 0x1BF2  # ingot graphic
    it.container = PLAYER_SERIAL
    it.layer = 0x15
    items = {0x40014AD1: it}
    assert _find_player_backpack(items, PLAYER_SERIAL) is None


def test_empty_world_returns_none():
    assert _find_player_backpack({}, PLAYER_SERIAL) is None
