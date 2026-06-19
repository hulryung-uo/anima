"""The recency tiebreak's age ordering must survive a genome-id WIDTH rollover.

choose_parent_for_target nudges toward the fresher lineage by ranking same-row
elites in genome-id (creation) order. The kernel mints ids as ``g_{n:05d}``, so
a plain lexicographic sort matches creation order only while every id is the
same width. Once the count passes 99999 the id grows to 6 digits and a string
sort INVERTS — ``"g_100000" < "g_99999"`` — so the NEWEST genome is treated as
the OLDEST and the freshest-lineage nudge points at stale code. select._id_order
sorts on the numeric suffix instead, keeping recency correct at any width.
"""
from foundry.kernel.archive import Genome
from foundry.select import _id_order, choose_parent_for_target


def _mk(gid: str, cell: tuple, per_seed: list[float]) -> Genome:
    return Genome(
        id=gid,
        eval={
            "fitness": sum(per_seed) / len(per_seed),
            "cell": list(cell),
            "per_seed_fitness": per_seed,
        },
    )


class _FakeArchive:
    def __init__(self, elites: list[Genome]) -> None:
        self._by_cell = {tuple(g.cell): g for g in elites}

    def elites(self) -> list[Genome]:
        return list(self._by_cell.values())

    def get_elite(self, cell: tuple):
        return self._by_cell.get(tuple(cell))


def test_id_order_is_numeric_not_lexicographic_across_width():
    older = "g_99999"    # genome #99999 (5 digits)
    newer = "g_100000"   # genome #100000 (6 digits) — created LATER
    # The bug we fix: a plain string sort puts the newer id FIRST (wrong).
    assert sorted([older, newer]) == [newer, older]
    # _id_order restores true creation order: older first, newer last.
    assert sorted([older, newer], key=_id_order) == [older, newer]
    assert _id_order(older) < _id_order(newer)


def test_recency_favours_the_truly_newer_genome_after_rollover():
    # Two equal-quality elites in the same profession row; one was created after
    # the 5→6 digit rollover. With equal quality the recency tiebreak decides,
    # and it must favour the genuinely newer (higher-numbered) genome.
    older = _mk("g_99999", ("COMBAT", 1), [24.0, 24.0, 24.0])
    newer = _mk("g_100000", ("COMBAT", 0), [24.0, 24.0, 24.0])
    arc = _FakeArchive([older, newer])

    counts = {"g_99999": 0, "g_100000": 0}
    for seed in range(400):
        pick = choose_parent_for_target(arc, ("COMBAT", 0), seed=seed)
        counts[pick.id] += 1
    # The newer genome (g_100000) must be drawn more often than the older one.
    assert counts["g_100000"] > counts["g_99999"], counts


def test_non_numeric_id_sorts_last_and_does_not_crash():
    # A hand/legacy id with no numeric suffix must still total-order (sorts as
    # "newest" via the sentinel) without raising.
    legacy = _mk("seed-root", ("COMBAT", 1), [10.0, 10.0, 10.0])
    numbered = _mk("g_00010", ("COMBAT", 0), [10.0, 10.0, 10.0])
    arc = _FakeArchive([legacy, numbered])
    # Just exercising the path: a draw must succeed for any seed.
    pick = choose_parent_for_target(arc, ("COMBAT", 0), seed=1)
    assert pick.id in {"seed-root", "g_00010"}
    assert _id_order("seed-root") > _id_order("g_00010")
