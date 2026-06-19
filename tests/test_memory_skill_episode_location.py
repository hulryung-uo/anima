"""A rewarded skill-execution episode must feed the location-value map and
action stats, not just the episodes table.

Regression: the skill-execution recording path called
``memory_db.record_episode`` directly, which wrote the episode row but never
fed ``update_location_value`` or ``update_action_stats``. Since the only other
caller of the shared ``_record_episode`` helper produced reward-0.0 ``speak``
episodes (which the location-value feed explicitly skips), the whole
location-value channel and the per-context action stats stayed empty. The fix
routes skill episodes through ``_record_episode`` so combat/gathering/crafting
rewards actually populate those learned-memory channels.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.brain.think import _record_episode
from anima.memory.database import MemoryDB
from anima.skills.state import region_coords


def _make_ctx(memory_db: MemoryDB, x: int, y: int) -> SimpleNamespace:
    """Minimal BrainContext stand-in sufficient for _record_episode."""
    self_state = SimpleNamespace(x=x, y=y, hp_percent=100, serial=1)
    world = SimpleNamespace(nearby_mobiles=lambda *a, **k: [])
    perception = SimpleNamespace(self_state=self_state, world=world)
    persona = SimpleNamespace(name="Anima")
    cfg = SimpleNamespace(memory=SimpleNamespace(max_episodes=1000))
    return SimpleNamespace(
        memory_db=memory_db,
        perception=perception,
        blackboard={"persona": persona},
        llm=None,
        cfg=cfg,
    )


@pytest.fixture
async def db():
    memory = MemoryDB(":memory:")
    await memory.init()
    yield memory
    await memory.close()


@pytest.mark.asyncio
async def test_rewarded_skill_episode_feeds_location_value_map(db: MemoryDB) -> None:
    x, y = 1500, 1620
    ctx = _make_ctx(db, x, y)

    # Simulate the skill-execution recording path (combat win with a real reward).
    await _record_episode(
        ctx,
        "melee_attack",
        "slew an orc",
        "success",
        8.0,
        summary="slew an orc",
    )

    # The episode itself is recorded...
    episodes = await db.query_episodes("Anima", limit=5)
    assert len(episodes) == 1
    assert episodes[0].action == "melee_attack"
    assert episodes[0].reward == 8.0

    # ...and crucially the location-value map for this region now knows the
    # activity paid off here (previously this stayed empty).
    rx, ry = region_coords(x, y)
    loc_values = await db.get_location_values("Anima", rx, ry)
    assert loc_values, "rewarded skill episode must populate the location-value map"
    activity, total_reward, visits = loc_values[0]
    assert activity == "melee_attack"
    assert total_reward == pytest.approx(8.0)
    assert visits == 1

    # get_best_locations (the recommendation accessor) now surfaces this region.
    best = await db.get_best_locations("Anima", "melee_attack")
    assert (rx, ry) in [(bx, by) for bx, by, _avg, _v in best]


@pytest.mark.asyncio
async def test_rewarded_skill_episode_feeds_action_stats(db: MemoryDB) -> None:
    ctx = _make_ctx(db, 1500, 1620)

    await _record_episode(ctx, "mine_ore", "iron vein", "success", 3.0)
    await _record_episode(ctx, "mine_ore", "empty vein", "failure", -1.0)

    # hp_percent=100, no players nearby -> "exploring" context bucket.
    stats = await db.get_action_stats("Anima", "exploring")
    by_action = {s.action: s for s in stats}
    assert "mine_ore" in by_action, "skill episodes must feed action stats"
    mine = by_action["mine_ore"]
    assert mine.successes == 1
    assert mine.failures == 1
    assert mine.total_reward == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_zero_reward_episode_does_not_pollute_location_map(db: MemoryDB) -> None:
    """Reward-0.0 episodes (e.g. neutral speech) carry no learning signal and
    must not dilute the per-visit averages the read path ranks by."""
    x, y = 1500, 1620
    ctx = _make_ctx(db, x, y)

    await _record_episode(ctx, "speak", "hello", "success", 0.0)

    rx, ry = region_coords(x, y)
    loc_values = await db.get_location_values("Anima", rx, ry)
    assert loc_values == []
