"""``_junk_ore_serials`` must stay bounded over a long mining/smelting soak.

When smelting blacklists a colored-ore hue, ``SmeltOre`` drops the unsmelable
stacks on the ground and records their item serials in the shared
``_junk_ore_serials`` blackboard set so the planner's pickup loops skip them.
Ore item serials are DISTINCT per stack and stream unboundedly over a soak
(every freshly mined pile gets a new serial; a dropped pile decays/despawns
without re-streaming), and the set is persisted in the planner snapshot — so it
used to grow one entry per dropped junk stack for the whole session and survive
reconnects. ``_mark_junk_ore`` now caps it the same way combat_loop caps
``_looted_corpses``.
"""
from anima.procedures.smelt_ore import JUNK_ORE_CACHE_MAX, _mark_junk_ore


def test_junk_ore_serials_stay_bounded_over_a_soak():
    """Blacklisting many hues' worth of distinct dropped stacks keeps the
    de-dup set bounded instead of leaking one entry per stack forever."""
    blackboard: dict = {}

    drops = JUNK_ORE_CACHE_MAX * 5
    serial = 0
    for _ in range(drops):
        # Each blacklist event drops a few distinct fresh-serial ore stacks.
        batch = []
        for _ in range(3):
            serial += 1
            batch.append(serial)
        _mark_junk_ore(blackboard, batch)

    junk = blackboard["_junk_ore_serials"]
    # Without the cap this would equal drops*3; the reap keeps it bounded.
    assert len(junk) <= JUNK_ORE_CACHE_MAX
    # Sanity: the soak really overflowed the cap many times over.
    assert drops * 3 > JUNK_ORE_CACHE_MAX


def test_recent_junk_serial_still_deduped_under_the_cap():
    """Below the cap, a just-marked serial is retained (the de-dup behavior the
    set exists for is preserved)."""
    blackboard: dict = {}
    _mark_junk_ore(blackboard, [0xABCDEF])
    assert 0xABCDEF in blackboard["_junk_ore_serials"]
    # Marking it again is idempotent (set semantics) and does not grow the set.
    _mark_junk_ore(blackboard, [0xABCDEF])
    assert blackboard["_junk_ore_serials"] == {0xABCDEF}


def test_mark_junk_ore_creates_set_when_absent():
    """First call seeds the blackboard key with a set (setdefault path)."""
    blackboard: dict = {}
    returned = _mark_junk_ore(blackboard, [1, 2, 3])
    assert returned is blackboard["_junk_ore_serials"]
    assert blackboard["_junk_ore_serials"] == {1, 2, 3}


def test_mark_junk_ore_merges_into_existing_set():
    """An existing (e.g. snapshot-restored) set is extended, not replaced."""
    blackboard: dict = {"_junk_ore_serials": {10, 20}}
    existing = blackboard["_junk_ore_serials"]
    returned = _mark_junk_ore(blackboard, [30])
    assert returned is existing  # same object, mutated in place
    assert blackboard["_junk_ore_serials"] == {10, 20, 30}
