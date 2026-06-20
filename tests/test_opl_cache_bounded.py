"""The OPL name/property caches must be bounded across a long soak.

opl_names/opl_properties/opl_revisions deliberately survive remove() so a
despawned-then-re-seen entity keeps its learned name. But a long session streams
an unbounded number of DISTINCT serials (every Spawner mob, ground-loot pile and
corpse is OPL'd once then despawns forever) and nothing reaps those rows — so the
caches used to grow without bound. These tests pin the FIFO-capped behaviour.
"""

from anima.perception.world_state import WorldState


class TestOplCacheBounded:
    def test_names_cache_never_exceeds_cap(self):
        ws = WorldState()
        ws.OPL_CACHE_MAX = 8  # shrink for a fast test
        for serial in range(100):  # far more distinct serials than the cap
            ws.remember_opl_name(serial, f"mob-{serial}")
        assert len(ws.opl_names) == 8

    def test_properties_and_revisions_also_capped(self):
        ws = WorldState()
        ws.OPL_CACHE_MAX = 5
        for serial in range(50):
            ws.remember_opl_properties(serial, [f"prop-{serial}"])
            ws.remember_opl_revision(serial, serial)
        assert len(ws.opl_properties) == 5
        assert len(ws.opl_revisions) == 5

    def test_fifo_evicts_oldest_serial(self):
        ws = WorldState()
        ws.OPL_CACHE_MAX = 3
        for serial in (1, 2, 3):
            ws.remember_opl_name(serial, f"n{serial}")
        ws.remember_opl_name(4, "n4")  # overflow -> evicts serial 1 (oldest)
        assert 1 not in ws.opl_names
        assert set(ws.opl_names) == {2, 3, 4}

    def test_rewrite_keeps_serial_hot_against_eviction(self):
        ws = WorldState()
        ws.OPL_CACHE_MAX = 3
        for serial in (1, 2, 3):
            ws.remember_opl_name(serial, f"n{serial}")
        # Refresh serial 1's name: it must become most-recent, so the NEXT
        # overflow evicts serial 2 (now the oldest), not the just-refreshed 1.
        ws.remember_opl_name(1, "n1-fresh")
        ws.remember_opl_name(4, "n4")
        assert 1 in ws.opl_names
        assert ws.opl_names[1] == "n1-fresh"
        assert 2 not in ws.opl_names

    def test_cached_name_still_restored_within_cap(self):
        # The cache's whole purpose: a re-seen mobile recovers its learned name.
        ws = WorldState()
        ws.remember_opl_name(0x1234, "Elara the Healer")
        ws.remove(0x1234)  # despawn — cache deliberately survives
        mob = ws.get_or_create_mobile(0x1234)  # re-seen
        assert mob.name == "Elara the Healer"
