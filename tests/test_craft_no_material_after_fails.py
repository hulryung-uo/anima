"""A blacksmith batch that consumes ingots and then breaks on a terminal
``no_material`` / ``no_station`` stop reason must be classified by that stop
reason — NOT swallowed by the generic ``fails > 0 or ingots consumed``
skill-check branch.

The bug: the skill-check branch ran first, so any batch that lost ingots
(the common case — a multi-ingot recipe whose attempts mostly miss) returned
a retryable ``BLOCKED`` result with ``next_suggestion="craft_blacksmith"`` and
never reached the ``no_material`` cooldown / circuit-breaker logic. A smith
with a stale Gold material context (server keeps replying "insufficient
metal") therefore looped forever on a craft it could not land, and the
material breaker never tripped to route it to selling instead.
"""

from types import SimpleNamespace

import pytest

import anima.procedures.craft_blacksmith as cb
from anima.planner.circuit_breaker import CircuitBreaker
from anima.procedures.base import FailureReason
from anima.procedures.craft_blacksmith import CraftBlacksmith


def _make_ctx():
    ss = SimpleNamespace(
        equipment={0x15: 0x40000015},
        skills={},
        gumps={},
        x=100,
        y=100,
    )
    return SimpleNamespace(
        perception=SimpleNamespace(
            self_state=ss,
            world=SimpleNamespace(items={}),
            social=SimpleNamespace(journal=[], recent=lambda count=8: []),
        ),
        conn=SimpleNamespace(connected=True),
        bus=None,
        blackboard={},
    )


@pytest.mark.asyncio
async def test_no_material_after_ingot_loss_trips_breaker(monkeypatch):
    """no_material stop with consumed ingots → MISSING_RESOURCE + breaker hit."""
    proc = CraftBlacksmith()

    # Stub out everything except the post-batch classification under test.
    monkeypatch.setattr(cb, "find_in_backpack", lambda ctx, g: [object()])  # tongs
    monkeypatch.setattr(cb, "_has_anvil_and_forge", lambda ctx: True)
    monkeypatch.setattr(
        CraftBlacksmith, "_pick_recipe",
        lambda self, ctx: ("Longsword", 3, 7, 12),
    )

    async def _noop_close(self, ctx):
        return None
    monkeypatch.setattr(CraftBlacksmith, "_close_all_gumps", _noop_close)

    async def _nav_ok(self, ctx, grp, idx):
        return None  # attempt sent, result pending
    monkeypatch.setattr(CraftBlacksmith, "_open_gump_and_craft", _nav_ok)

    # The batch's single resolved attempt reports the server's insufficient-
    # metal notice — but ingots were consumed (see _count_iron_ingots below),
    # so the buggy ordering would route through the skill-check branch first.
    state = {"resolved": False}

    async def _result(self, ctx, journal_mark, timeout=cb._RESULT_TIMEOUT_S):
        state["resolved"] = True
        return "no_material", "You do not have sufficient metal to make that."
    monkeypatch.setattr(CraftBlacksmith, "_await_craft_result", _result)

    # 20 ingots before the attempt resolves, 12 after (a failed multi-ingot
    # attempt ate 8 ingots before the server reported "insufficient metal").
    monkeypatch.setattr(
        cb, "_count_iron_ingots",
        lambda ctx: 12 if state["resolved"] else 20,
    )

    ctx = _make_ctx()
    breaker = CircuitBreaker(max_failures=3, cooldown_s=300.0)
    ctx.blackboard["_craft_material_breaker"] = breaker

    result = await proc.execute(ctx)

    # Classified as a material problem, not a generic skill-check miss.
    assert result.reason is FailureReason.MISSING_RESOURCE
    # Must NOT re-suggest crafting the same un-craftable item.
    assert result.next_suggestion != "craft_blacksmith"
    # The material breaker recorded the failure (it would have been skipped
    # entirely by the swallowed skill-check branch).
    assert breaker.failure_count("iron") == 1
    assert ctx.blackboard.get("_craft_bs_material_fails") == 1
