"""Mirror invariant: modes.py MODES must stay in lockstep with the planner.

modes.py promises (module docstring + line 47-48): "Profession loops mirror
anima/planner/planner.py:PROFESSION_LOOPS exactly so behaviour is unchanged
when P1 wires `loop` into selection." When the planner's adventurer loop was
extended with wander_for_combat + sell_to_vendor (the combat re-engage /
loot-flush fix), the combat Mode.loop silently kept the old
(bandage_self, hunt_nearby) pair. Once P1 wires Mode.loop into selection the
meta-controller would run the stale loop and reintroduce the mining-fallthrough
leak (and structurally-0 worth_term) the planner already eliminated. This test
pins the writer (planner.PROFESSION_LOOPS) and reader (modes.MODES) together.
"""

from anima.planner.modes import MODES, _PROFESSION_TO_MODE
from anima.planner.planner import PROFESSION_LOOPS


def test_combat_mode_mirrors_adventurer_loop():
    # The specific divergence this guards: the combat re-engage / loot-flush
    # extension must be present in BOTH definitions of the loop.
    assert MODES["combat"].loop == PROFESSION_LOOPS["adventurer"]
    assert "wander_for_combat" in MODES["combat"].loop
    assert "sell_to_vendor" in MODES["combat"].loop


def test_every_profession_mode_loop_mirrors_planner():
    """Each profession with an explicit planner loop maps to a mode whose
    non-empty `loop` reproduces that loop verbatim.

    Modes whose loop is empty () intentionally fall through to the planner's
    default ladder (mining + the living modes), so they are exempt — they make
    no behavioural claim to mirror.
    """
    for profession, loop in PROFESSION_LOOPS.items():
        mode_name = _PROFESSION_TO_MODE.get(profession)
        assert mode_name is not None, (
            f"profession {profession!r} has a planner loop but no mode mapping"
        )
        mode = MODES[mode_name]
        if not mode.loop:
            # empty loop = deliberate fall-through, not a mirror claim
            continue
        assert mode.loop == loop, (
            f"mode {mode_name!r} loop {mode.loop} diverged from "
            f"PROFESSION_LOOPS[{profession!r}] {loop}"
        )


def test_no_mode_loop_references_unknown_procedure():
    """Every procedure name a mode loop references is one the planner's loops
    also know — a typo on either side is a silent dead step."""
    known = set()
    for loop in PROFESSION_LOOPS.values():
        known.update(loop)
    # bandage_self appears via the living "rest" mode too; fold in every
    # procedure the planner loops legitimately use.
    for mode in MODES.values():
        for proc in mode.loop:
            assert proc in known, (
                f"mode {mode.name!r} references {proc!r} which no "
                f"PROFESSION_LOOPS entry uses"
            )
