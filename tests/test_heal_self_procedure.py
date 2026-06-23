"""Regression test: the planner's `heal_self` survival procedure is REGISTERED
and selectable, and its can_start gating is correct.

The planner does ``self.registry.get("heal_self")`` in two survival-critical
branches (backpack-less heal-in-place ~L948, Priority-1 critical-HP ladder
~L1166). Before this fix nothing registered a procedure under that name, so both
lookups silently returned None and the potion-quaff survival path was dead.

These tests stay narrow on purpose: they never drive ``planner.select_procedure``
(which dereferences ctx.conn and many other fields). They assert (a) a
ProcedureRegistry that registers HealSelf resolves the exact name the planner
looks up, and (b) can_start returns the expected bool for a built ctx — using a
tiny fake ctx that only carries the fields can_start touches (perception +
blackboard), never ctx.conn.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.perception import Perception
from anima.perception.world_state import ItemInfo
from anima.procedures.base import ProcedureRegistry
from anima.procedures.combat_loop import HEAL_POTION_GRAPHICS
from anima.procedures.heal_self import HealSelf

_BACKPACK_LAYER = 0x15
_BACKPACK_SERIAL = 0xDEAD0001
_HEAL_POTION_GRAPHIC = next(iter(HEAL_POTION_GRAPHICS))


def _build_ctx(*, hits: int, hits_max: int, has_potion: bool, poisoned: bool = False):
    """Minimal ctx carrying only what HealSelf.can_start touches.

    can_start reads ctx.perception.self_state (+ find_in_backpack, which reads
    ctx.perception) and ctx.blackboard. It never touches ctx.conn — so a bare
    SimpleNamespace with these two fields is a faithful, crash-free stand-in.
    """
    perception = Perception(player_serial=0x1000)
    ss = perception.self_state
    ss.serial = 0x1000
    ss.hits = hits
    ss.hits_max = hits_max
    ss.is_poisoned = poisoned
    ss.equipment[_BACKPACK_LAYER] = _BACKPACK_SERIAL
    if has_potion:
        perception.world.items[0xABCD] = ItemInfo(
            serial=0xABCD,
            graphic=_HEAL_POTION_GRAPHIC,
            container=_BACKPACK_SERIAL,
            amount=1,
        )
    return SimpleNamespace(perception=perception, blackboard={})


def test_heal_self_registered_under_planner_lookup_name():
    """The planner does registry.get("heal_self"); registering HealSelf must
    resolve under exactly that name (the bug: it never was)."""
    registry = ProcedureRegistry()
    registry.register(HealSelf())
    proc = registry.get("heal_self")
    assert proc is not None
    assert proc.name == "heal_self"
    assert isinstance(proc, HealSelf)


def test_avatar_registration_list_includes_heal_self():
    """Guard the production wiring in Avatar.create(): the heal_self procedure
    class must be imported and present so registry.get("heal_self") is live."""
    import inspect

    import anima.core.avatar as avatar_mod

    src = inspect.getsource(avatar_mod.Avatar.create)
    assert "HealSelf as HealSelfProc" in src, "heal_self proc not imported in create()"
    assert "HealSelfProc" in src.split("procedure_registry.register")[0]


@pytest.mark.asyncio
async def test_can_start_true_when_wounded_with_potion():
    proc = HealSelf()
    ctx = _build_ctx(hits=30, hits_max=100, has_potion=True)
    assert await proc.can_start(ctx) is True


@pytest.mark.asyncio
async def test_can_start_false_when_no_potion():
    proc = HealSelf()
    ctx = _build_ctx(hits=30, hits_max=100, has_potion=False)
    assert await proc.can_start(ctx) is False


@pytest.mark.asyncio
async def test_can_start_false_at_full_hp():
    proc = HealSelf()
    ctx = _build_ctx(hits=100, hits_max=100, has_potion=True)
    assert await proc.can_start(ctx) is False


@pytest.mark.asyncio
async def test_can_start_false_when_poisoned():
    """ServUO refuses a heal potion while poisoned — yield to the cure path."""
    proc = HealSelf()
    ctx = _build_ctx(hits=30, hits_max=100, has_potion=True, poisoned=True)
    assert await proc.can_start(ctx) is False


@pytest.mark.asyncio
async def test_can_start_false_inside_potion_cooldown():
    """A recent quaff parks _potion_last_ts; can_start must respect the use-delay."""
    import time

    proc = HealSelf()
    ctx = _build_ctx(hits=30, hits_max=100, has_potion=True)
    ctx.blackboard["_potion_last_ts"] = time.monotonic()
    assert await proc.can_start(ctx) is False
