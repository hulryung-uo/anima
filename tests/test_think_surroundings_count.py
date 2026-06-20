"""The think-prompt "surroundings" block must tally duplicate mob names and
never silently truncate a crowded scene.

Before the fix ``_build_surroundings`` listed every mob name verbatim and hard
sliced to the first five, so the survival arena (2 Ettins + 4 HeadlessOne) read
as "a headless one, a headless one, a headless one, a headless one, an ettin" —
the sixth mob vanished and the four identical ones looked like four distinct
foes. The strategist therefore under-weighted the danger exactly when survival
mattered. The fix groups identical names into "name xN" and appends the true
remaining count when there are more groups than spelled out.
"""

from __future__ import annotations

from types import SimpleNamespace

from anima.brain.think import _build_surroundings
from anima.perception.enums import NotorietyFlag


def _mob(name: str, notoriety: NotorietyFlag, *, hits: int = 50, hits_max: int = 50, body: int = 400):
    return SimpleNamespace(
        name=name, notoriety=notoriety, hits=hits, hits_max=hits_max, body=body,
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


def _line(out: str, prefix: str) -> str:
    return next(ln for ln in out.splitlines() if ln.startswith(prefix))


def test_duplicate_hostiles_are_tallied_not_repeated():
    ctx = _ctx([_mob("a headless one", NotorietyFlag.ENEMY) for _ in range(4)])
    line = _line(_build_surroundings(ctx), "Hostile nearby:")
    assert line == "Hostile nearby: a headless one x4"
    # The verbatim repeat is gone.
    assert "a headless one, a headless one" not in line


def test_survival_arena_threats_fully_accounted():
    # 2 Ettins + 4 HeadlessOne == the live arena composition. None may vanish.
    mobs = [_mob("an ettin", NotorietyFlag.ENEMY) for _ in range(2)]
    mobs += [_mob("a headless one", NotorietyFlag.ENEMY) for _ in range(4)]
    line = _line(_build_surroundings(ctx := _ctx(mobs)), "Hostile nearby:")
    assert "an ettin x2" in line
    assert "a headless one x4" in line
    # Two distinct groups, both shown — nothing dropped, no "more" needed.
    assert "more" not in line


def test_more_than_five_distinct_groups_surfaces_remaining_count():
    mobs = [_mob(f"orc {i}", NotorietyFlag.ENEMY) for i in range(7)]
    line = _line(_build_surroundings(_ctx(mobs)), "Hostile nearby:")
    # Five spelled out, the remaining two surfaced as a count (not dropped).
    assert "and 2 more" in line
    assert line.count(",") == 5  # 5 names + the trailing "and N more"


def test_single_distinct_name_unchanged():
    # Regression guard: distinct singletons keep their plain comma list (no xN).
    ctx = _ctx([
        _mob("an orc", NotorietyFlag.ENEMY),
        _mob("a thief", NotorietyFlag.CRIMINAL),
    ])
    line = _line(_build_surroundings(ctx), "Hostile nearby:")
    assert line == "Hostile nearby: an orc, a thief"


def test_friendly_duplicates_also_tallied():
    ctx = _ctx([_mob("a town guard", NotorietyFlag.INNOCENT) for _ in range(3)])
    line = _line(_build_surroundings(ctx), "People nearby:")
    assert line == "People nearby: a town guard x3"


def test_overflow_count_is_mobs_not_groups():
    """The "and N more" tail must count hidden MOBILES, not hidden groups.

    Five distinct singletons fill the spelled-out quota; a sixth group of four
    identical foes overflows. Counting the dropped *group* ("and 1 more") would
    erase three of the four — exactly the danger under-report the summary is
    meant to prevent. The tail must read "and 4 more".
    """
    mobs = [_mob(f"orc {i}", NotorietyFlag.ENEMY) for i in range(5)]
    mobs += [_mob("a headless one", NotorietyFlag.ENEMY) for _ in range(4)]
    line = _line(_build_surroundings(_ctx(mobs)), "Hostile nearby:")
    assert "and 4 more" in line
    assert "and 1 more" not in line


def test_overflow_count_sums_multiple_hidden_groups():
    """Several hidden multi-count groups all contribute to the overflow tally."""
    mobs = [_mob(f"rat {i}", NotorietyFlag.ENEMY) for i in range(5)]
    mobs += [_mob("an ettin", NotorietyFlag.ENEMY) for _ in range(2)]
    mobs += [_mob("a troll", NotorietyFlag.ENEMY) for _ in range(3)]
    line = _line(_build_surroundings(_ctx(mobs)), "Hostile nearby:")
    # 2 ettins + 3 trolls hidden behind the 5 spelled-out rats == 5 more.
    assert "and 5 more" in line
