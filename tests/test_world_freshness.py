"""Tests for world-state freshness: stale-mobile pruning + last_seen stamping."""

from anima.perception.world_state import WorldState


class TestStaleMobilePruning:
    def test_get_or_create_stamps_last_seen(self):
        ws = WorldState()
        mob = ws.get_or_create_mobile(0x1234)
        assert mob.last_seen > 0.0

    def test_touch_refreshes_last_seen(self):
        ws = WorldState()
        mob = ws.get_or_create_mobile(0x1234)
        first = mob.last_seen
        mob.last_seen = first - 100.0  # simulate an old stamp
        again = ws.get_or_create_mobile(0x1234)  # a later packet touches it
        assert again is mob
        assert again.last_seen > first - 100.0

    def test_prune_removes_stale_mobile(self):
        ws = WorldState()
        mob = ws.get_or_create_mobile(0x1234)
        mob.last_seen = 1000.0
        # 31s later with a 30s TTL -> stale
        pruned = ws.prune_stale_mobiles(now=1031.0, max_age=30.0)
        assert pruned == [0x1234]
        assert 0x1234 not in ws.mobiles

    def test_prune_keeps_fresh_mobile(self):
        ws = WorldState()
        mob = ws.get_or_create_mobile(0x1234)
        mob.last_seen = 1000.0
        # only 5s later -> fresh
        pruned = ws.prune_stale_mobiles(now=1005.0, max_age=30.0)
        assert pruned == []
        assert 0x1234 in ws.mobiles

    def test_prune_skips_never_stamped(self):
        # A mobile constructed directly (e.g. a test fixture or a record that
        # bypassed get_or_create_mobile) has last_seen == 0.0 and must NOT be
        # reaped just because "now" is large.
        ws = WorldState()
        from anima.perception.world_state import MobileInfo

        ws.mobiles[0x99] = MobileInfo(serial=0x99)
        assert ws.mobiles[0x99].last_seen == 0.0
        pruned = ws.prune_stale_mobiles(now=1_000_000.0, max_age=30.0)
        assert pruned == []
        assert 0x99 in ws.mobiles

    def test_prune_phantom_not_returned_by_nearby(self):
        # The end-to-end freshness guarantee: a stale phantom parked at the
        # player's new coordinates must not survive a prune and thus must not
        # be reported by nearby_mobiles.
        ws = WorldState()
        phantom = ws.get_or_create_mobile(0xDEAD)
        phantom.x, phantom.y = 200, 200
        phantom.last_seen = 1000.0
        # Player recalls to (200, 200); server never sent 0x1D for the phantom.
        assert any(m.serial == 0xDEAD for m in ws.nearby_mobiles(200, 200))
        ws.prune_stale_mobiles(now=1100.0, max_age=30.0)
        assert not any(m.serial == 0xDEAD for m in ws.nearby_mobiles(200, 200))

    def test_prune_multiple_mixed_ages(self):
        ws = WorldState()
        old1 = ws.get_or_create_mobile(0x01)
        old1.last_seen = 1000.0
        fresh = ws.get_or_create_mobile(0x02)
        fresh.last_seen = 1090.0
        old2 = ws.get_or_create_mobile(0x03)
        old2.last_seen = 1010.0
        pruned = set(ws.prune_stale_mobiles(now=1100.0, max_age=30.0))
        assert pruned == {0x01, 0x03}
        assert set(ws.mobiles) == {0x02}
