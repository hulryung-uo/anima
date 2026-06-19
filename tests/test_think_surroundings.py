"""The think-prompt "surroundings" block must not present a hostile mobile as a
friendly "People nearby" entry.

Before the fix ``_build_surroundings`` rendered every nearby mobile under one
"People nearby:" line regardless of notoriety, so a red murderer (MURDERER),
an orange ENEMY monster, or a gray criminal read identically to a town NPC.
The LLM strategist (which picks go/explore/idle/speak) therefore got no danger
signal at all. The fix splits hostiles onto their own "Hostile nearby:" line
and drops dead ghosts from both lists.
"""

from __future__ import annotations

from types import SimpleNamespace

from anima.brain.think import _build_surroundings
from anima.perception.enums import NotorietyFlag


def _mob(name: str, notoriety: NotorietyFlag, *, hits: int = 50, hits_max: int = 50, body: int = 400):
    # is_dead reads body/hits/hits_max; default to a living human.
    return SimpleNamespace(
        name=name, notoriety=notoriety, hits=hits, hits_max=hits_max, body=body,
        # is_dead is a property on the real MobileInfo; emulate it here.
        is_dead=(hits_max > 0 and hits <= 0),
    )


def _ctx(mobs: list) -> SimpleNamespace:
    ss = SimpleNamespace(x=100, y=100)
    world = SimpleNamespace(
        nearby_items=lambda x, y, distance=18: [],
        nearby_mobiles=lambda x, y, distance=18: mobs,
    )
    perception = SimpleNamespace(self_state=ss, world=world)
    return SimpleNamespace(perception=perception)


def test_hostile_mob_not_rendered_as_friendly_person():
    ctx = _ctx([_mob("a fierce ettin", NotorietyFlag.ENEMY)])
    out = _build_surroundings(ctx)
    # The threat is announced on its own line...
    assert "Hostile nearby: a fierce ettin" in out
    # ...and is NOT laundered into the friendly "People nearby" line.
    assert "People nearby" not in out


def test_murderer_is_hostile():
    ctx = _ctx([_mob("Bloodhand", NotorietyFlag.MURDERER)])
    out = _build_surroundings(ctx)
    assert "Hostile nearby: Bloodhand" in out
    assert "People nearby" not in out


def test_innocent_is_friendly():
    ctx = _ctx([_mob("Hastin the baker", NotorietyFlag.INNOCENT)])
    out = _build_surroundings(ctx)
    assert "People nearby: Hastin the baker" in out
    assert "Hostile nearby" not in out


def test_threats_and_friendlies_split_onto_separate_lines():
    ctx = _ctx([
        _mob("a town guard", NotorietyFlag.INNOCENT),
        _mob("an orc", NotorietyFlag.ENEMY),
        _mob("a thief", NotorietyFlag.CRIMINAL),
    ])
    out = _build_surroundings(ctx)
    assert "Hostile nearby:" in out
    assert "an orc" in out and "a thief" in out
    assert "People nearby: a town guard" in out
    # The guard must stay out of the hostile line.
    hostile_line = next(ln for ln in out.splitlines() if ln.startswith("Hostile nearby:"))
    assert "a town guard" not in hostile_line


def test_dead_ghost_dropped_from_both_lists():
    # hits<=0 with hits_max>0 → is_dead. A corpse is neither a person to talk
    # to nor a live threat.
    ctx = _ctx([_mob("a corpse", NotorietyFlag.INNOCENT, hits=0, hits_max=50)])
    out = _build_surroundings(ctx)
    assert out == "Nothing notable nearby."


def test_nameless_hostile_gets_generic_label():
    ctx = _ctx([_mob("", NotorietyFlag.ENEMY)])
    out = _build_surroundings(ctx)
    assert "Hostile nearby: something" in out
