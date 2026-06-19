"""The action-stats context bucket must not mislabel monsters as players.

``_infer_context_pattern`` keys the action-stats reward buckets the LLM reads
back (``Past experience (near_player|exploring|low_hp)``). It used to detect a
"player nearby" via ``notoriety.value <= 6``, which matches INNOCENT(1) through
MURDERER(6) — i.e. every mobile except an INVULNERABLE town NPC. So a solo
grinder surrounded by ATTACKABLE/ENEMY/MURDERER monsters was bucketed under
``near_player`` instead of ``exploring``, polluting one bucket with combat
episodes and starving the other. Player detection must gate on the human body
(the same fix already proven in ``state._player_presence``), and the write-path
(brain.think) and read-path (memory.retrieval) inferers must agree.
"""

from __future__ import annotations

from types import SimpleNamespace

from anima.brain.think import _infer_context_pattern as think_infer
from anima.memory.retrieval import _infer_context_pattern as retrieval_infer
from anima.perception.enums import NotorietyFlag
from anima.skills.state import has_player_nearby

HUMAN_BODY = 0x0190  # male player body
MONSTER_BODY = 0x00D6  # ettin-ish gigantic body (never a player)


def _mob(notoriety: NotorietyFlag, *, body: int, serial: int) -> SimpleNamespace:
    return SimpleNamespace(notoriety=notoriety, body=body, serial=serial)


def _ctx(mobs: list, *, hp_percent: int = 100) -> SimpleNamespace:
    ss = SimpleNamespace(x=100, y=100, serial=1, hp_percent=hp_percent)
    world = SimpleNamespace(
        nearby_mobiles=lambda x, y, distance=18: mobs,
        nearby_items=lambda x, y, distance=18: [],
    )
    perception = SimpleNamespace(self_state=ss, world=world)
    return SimpleNamespace(perception=perception)


def test_field_of_monsters_is_exploring_not_near_player():
    # A solo grinder among hostile mobs — no actual players present.
    mobs = [
        _mob(NotorietyFlag.ATTACKABLE, body=MONSTER_BODY, serial=10),
        _mob(NotorietyFlag.ENEMY, body=MONSTER_BODY, serial=11),
        _mob(NotorietyFlag.MURDERER, body=MONSTER_BODY, serial=12),
    ]
    ctx = _ctx(mobs)
    assert has_player_nearby(ctx) is False
    assert think_infer(ctx) == "exploring"
    assert retrieval_infer(ctx) == "exploring"


def test_human_bodied_mobile_is_near_player():
    # An actual player (human body) standing nearby.
    mobs = [_mob(NotorietyFlag.INNOCENT, body=HUMAN_BODY, serial=20)]
    ctx = _ctx(mobs)
    assert has_player_nearby(ctx) is True
    assert think_infer(ctx) == "near_player"
    assert retrieval_infer(ctx) == "near_player"


def test_self_excluded_by_serial():
    # The agent's own human body must not count as "another player".
    mobs = [_mob(NotorietyFlag.INNOCENT, body=HUMAN_BODY, serial=1)]
    ctx = _ctx(mobs)
    assert has_player_nearby(ctx) is False
    assert think_infer(ctx) == "exploring"


def test_low_hp_takes_priority():
    mobs = [_mob(NotorietyFlag.ENEMY, body=MONSTER_BODY, serial=30)]
    ctx = _ctx(mobs, hp_percent=20)
    assert think_infer(ctx) == "low_hp"
    assert retrieval_infer(ctx) == "low_hp"


def test_write_and_read_inferers_agree():
    # The two code paths must produce identical buckets for the same state, or
    # update_action_stats writes to one key and retrieve_context reads another.
    scenarios = [
        [_mob(NotorietyFlag.ENEMY, body=MONSTER_BODY, serial=40)],
        [_mob(NotorietyFlag.INNOCENT, body=HUMAN_BODY, serial=41)],
        [_mob(NotorietyFlag.ATTACKABLE, body=MONSTER_BODY, serial=42),
         _mob(NotorietyFlag.INNOCENT, body=HUMAN_BODY, serial=43)],
        [],
    ]
    for mobs in scenarios:
        ctx = _ctx(mobs)
        assert think_infer(ctx) == retrieval_infer(ctx)
