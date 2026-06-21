"""The Q-table ``_enemy_presence`` threat set must match the combat loop.

``encode_state`` keys the RL state on whether a *threat* is nearby. The set of
notoriety values that count as a threat must be the SAME set the agent actually
fights (``combat_loop.ATTACKABLE_NOTORIETY``) and the LLM strategist reads as
danger (``brain.think._HOSTILE_NOTORIETY``) — namely ATTACKABLE(3), CRIMINAL(4),
ENEMY(5), MURDERER(6). A regression that dropped CRIMINAL(4) made a lone gray
criminal encode the state key as ``safe`` while the combat loop was engaging it,
corrupting the RL state space and softening survival/healing gating. These tests
pin the agreement so the sets cannot silently drift apart again.
"""

from __future__ import annotations

from types import SimpleNamespace

from anima.perception.enums import NotorietyFlag
from anima.skills.state import _enemy_presence

MONSTER_BODY = 0x00D6  # never a player body


def _mob(notoriety: NotorietyFlag, *, is_dead: bool = False) -> SimpleNamespace:
    return SimpleNamespace(notoriety=notoriety, body=MONSTER_BODY,
                           serial=10, is_dead=is_dead)


def _ctx(mobs: list) -> SimpleNamespace:
    ss = SimpleNamespace(x=100, y=100, serial=1)
    world = SimpleNamespace(nearby_mobiles=lambda x, y, distance=18: mobs)
    perception = SimpleNamespace(self_state=ss, world=world)
    return SimpleNamespace(perception=perception)


def test_criminal_only_mob_reads_as_enemy():
    """The regression case: a lone gray CRIMINAL(4) is a threat, not 'safe'."""
    ctx = _ctx([_mob(NotorietyFlag.CRIMINAL)])
    assert _enemy_presence(ctx) == "enemies"


def test_all_attackable_notorieties_read_as_enemy():
    for noto in (NotorietyFlag.ATTACKABLE, NotorietyFlag.CRIMINAL,
                 NotorietyFlag.ENEMY, NotorietyFlag.MURDERER):
        ctx = _ctx([_mob(noto)])
        assert _enemy_presence(ctx) == "enemies", noto


def test_friendly_notorieties_read_as_safe():
    for noto in (NotorietyFlag.INNOCENT, NotorietyFlag.ALLY,
                 NotorietyFlag.INVULNERABLE):
        ctx = _ctx([_mob(noto)])
        assert _enemy_presence(ctx) == "safe", noto


def test_dead_criminal_reads_as_safe():
    """A felled mob keeping its hostile notoriety must not pin the key open."""
    ctx = _ctx([_mob(NotorietyFlag.CRIMINAL, is_dead=True)])
    assert _enemy_presence(ctx) == "safe"


def test_enemy_set_matches_combat_loop_and_strategist():
    """The three modules must agree on which notorieties are threats."""
    from anima.brain.think import _HOSTILE_NOTORIETY
    from anima.procedures.combat_loop import ATTACKABLE_NOTORIETY

    expected = {
        NotorietyFlag.ATTACKABLE,
        NotorietyFlag.CRIMINAL,
        NotorietyFlag.ENEMY,
        NotorietyFlag.MURDERER,
    }
    assert set(ATTACKABLE_NOTORIETY) == expected
    assert set(_HOSTILE_NOTORIETY) == expected

    # And _enemy_presence agrees member-for-member with that shared set.
    for noto in NotorietyFlag:
        ctx = _ctx([_mob(noto)])
        got = _enemy_presence(ctx) == "enemies"
        assert got == (noto in expected), noto
