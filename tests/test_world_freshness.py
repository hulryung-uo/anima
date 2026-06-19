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

    def test_prune_cascades_worn_items(self):
        # A stale mobile's worn equipment / backpack contents (container ==
        # mobile serial, as set by 0x78 MobileIncoming) must be dropped when
        # the mobile is pruned. Otherwise they orphan in world.items with a
        # dangling container pointer and leak across a long eval, exactly the
        # leak remove() guards against for an explicit 0x1D Delete.
        ws = WorldState()
        mob = ws.get_or_create_mobile(0xABCD)
        mob.last_seen = 1000.0
        # A worn weapon directly on the mobile, and a backpack holding an item
        # nested one level deeper.
        weapon = ws.get_or_create_item(0x100)
        weapon.container = 0xABCD
        backpack = ws.get_or_create_item(0x200)
        backpack.container = 0xABCD
        loot = ws.get_or_create_item(0x300)
        loot.container = 0x200  # inside the backpack
        # An unrelated ground item that must survive.
        bystander = ws.get_or_create_item(0x400)
        bystander.container = 0

        pruned = ws.prune_stale_mobiles(now=1100.0, max_age=30.0)

        assert pruned == [0xABCD]
        assert 0xABCD not in ws.mobiles
        # The entire worn/contained subtree is gone, including the nested loot.
        assert 0x100 not in ws.items
        assert 0x200 not in ws.items
        assert 0x300 not in ws.items
        # The unrelated ground item is untouched.
        assert 0x400 in ws.items

    def test_prune_keeps_fresh_mobiles_items(self):
        # Pruning a stale mobile must NOT collaterally drop a fresh mobile's
        # worn items.
        ws = WorldState()
        fresh = ws.get_or_create_mobile(0x01)
        fresh.last_seen = 1090.0
        worn = ws.get_or_create_item(0x10)
        worn.container = 0x01
        stale = ws.get_or_create_mobile(0x02)
        stale.last_seen = 1000.0
        ws.prune_stale_mobiles(now=1100.0, max_age=30.0)
        assert 0x01 in ws.mobiles
        assert 0x10 in ws.items
