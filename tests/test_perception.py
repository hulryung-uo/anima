"""Tests for perception data models."""

from anima.perception.event_stream import EventStream, GameEventType
from anima.perception.self_state import SelfState
from anima.perception.social_state import SocialState
from anima.perception.world_state import WorldState

# ---------------------------------------------------------------------------
# WorldState
# ---------------------------------------------------------------------------


class TestWorldState:
    def test_get_or_create_mobile(self):
        ws = WorldState()
        mob = ws.get_or_create_mobile(0x00001234)
        assert mob.serial == 0x00001234
        # Second call returns same object
        mob2 = ws.get_or_create_mobile(0x00001234)
        assert mob2 is mob

    def test_get_or_create_item(self):
        ws = WorldState()
        item = ws.get_or_create_item(0x40001234)
        assert item.serial == 0x40001234
        item2 = ws.get_or_create_item(0x40001234)
        assert item2 is item

    def test_remove(self):
        ws = WorldState()
        ws.get_or_create_mobile(0x01)
        ws.get_or_create_item(0x02)
        ws.remove(0x01)
        assert 0x01 not in ws.mobiles
        ws.remove(0x02)
        assert 0x02 not in ws.items

    def test_remove_nonexistent(self):
        ws = WorldState()
        ws.remove(0x9999)  # should not raise

    def test_nearby_mobiles(self):
        ws = WorldState()
        m1 = ws.get_or_create_mobile(1)
        m1.x, m1.y = 100, 100
        m2 = ws.get_or_create_mobile(2)
        m2.x, m2.y = 110, 110
        m3 = ws.get_or_create_mobile(3)
        m3.x, m3.y = 500, 500  # far away

        nearby = ws.nearby_mobiles(105, 105, distance=18)
        serials = {m.serial for m in nearby}
        assert 1 in serials
        assert 2 in serials
        assert 3 not in serials

    def test_nearby_items_ground_only(self):
        ws = WorldState()
        i1 = ws.get_or_create_item(1)
        i1.x, i1.y, i1.container = 100, 100, 0
        i2 = ws.get_or_create_item(2)
        i2.x, i2.y, i2.container = 100, 100, 0x1234  # in a container

        nearby = ws.nearby_items(100, 100)
        assert len(nearby) == 1
        assert nearby[0].serial == 1

    def test_mobile_name_persists_across_remove_and_recreate(self):
        # NPCs leave view via 0x1D Delete and re-enter via 0x78 MobileIncoming
        # when the agent walks away and comes back. Names/properties must
        # survive — they drive _is_vendor / _is_refused matching, and
        # re-requesting OPL races the planner's synchronous can_start checks.
        ws = WorldState()
        serial = 0x000008C2
        mob = ws.get_or_create_mobile(serial)
        mob.name = "Veda the provisioner"
        mob.properties = ["Veda the provisioner", "Minoc Provisioner"]
        ws.opl_names[serial] = mob.name
        ws.opl_properties[serial] = list(mob.properties)

        ws.remove(serial)
        assert serial not in ws.mobiles
        assert ws.opl_names.get(serial) == "Veda the provisioner"

        mob2 = ws.get_or_create_mobile(serial)
        assert mob2.name == "Veda the provisioner"
        assert mob2.properties == ["Veda the provisioner", "Minoc Provisioner"]

    def test_item_name_persists_across_remove_and_recreate(self):
        # 0xD6 MegaCliloc is the ONLY item-name source and arrives independently
        # of the item's spatial packets. An item can leave view (0x1D Delete)
        # and re-enter via 0x1A/0x3C/0x25 — which recreate a BLANK ItemInfo — or
        # the OPL can arrive before the item exists. handle_mega_cliloc caches
        # name/properties by serial (for items too), and the cache survives
        # remove(); get_or_create_item must restore from it like the mobile path
        # does, else name-keyed item lookups (loot/reagent/vendor) race a blank.
        ws = WorldState()
        serial = 0x40001234
        item = ws.get_or_create_item(serial)
        item.name = "spider's silk"
        item.properties = ["spider's silk", "Weight: 1 Stone"]
        ws.opl_names[serial] = item.name
        ws.opl_properties[serial] = list(item.properties)

        ws.remove(serial)
        assert serial not in ws.items
        # Cache intentionally survives remove() (see get_or_create_mobile note).
        assert ws.opl_names.get(serial) == "spider's silk"

        item2 = ws.get_or_create_item(serial)
        assert item2.name == "spider's silk"
        assert item2.properties == ["spider's silk", "Weight: 1 Stone"]


# ---------------------------------------------------------------------------
# SelfState
# ---------------------------------------------------------------------------


class TestSelfState:
    def test_hp_percent(self):
        s = SelfState(serial=0x01)
        s.hits = 50
        s.hits_max = 100
        assert s.hp_percent == 50.0

    def test_hp_percent_zero_max(self):
        s = SelfState(serial=0x01)
        assert s.hp_percent == 100.0

    def test_mana_percent(self):
        s = SelfState(serial=0x01)
        s.mana = 30
        s.mana_max = 100
        assert s.mana_percent == 30.0

    def test_stam_percent(self):
        s = SelfState(serial=0x01)
        s.stam = 75
        s.stam_max = 100
        assert s.stam_percent == 75.0

    def test_is_alive(self):
        s = SelfState(serial=0x01)
        s.hits = 50
        s.hits_max = 100
        assert s.is_alive is True
        s.hits = 0
        assert s.is_alive is False

    def test_is_alive_ghost_body_overrides_stale_health_bar(self):
        # Regression: on death ServUO flips the player to a ghost body, but a
        # stale / out-of-order 0xA1 can still report the pre-death hits. The
        # body flip is authoritative (ClassicUO Mobile.IsDead), so is_alive
        # must report dead even though hits > 0.
        s = SelfState(serial=0x01)
        s.hits = 50
        s.hits_max = 100
        s.body = 0x0192  # on-foot human ghost
        assert s.is_ghost is True
        assert s.is_alive is False

    def test_is_alive_mounted_ghost_body(self):
        # Dying while mounted/flying yields a different ghost graphic; it must
        # still count as dead (matches WorldState._GHOST_BODIES coverage).
        s = SelfState(serial=0x01)
        s.hits = 1
        s.hits_max = 100
        s.body = 0x02B6  # mounted ghost
        assert s.is_ghost is True
        assert s.is_alive is False

    def test_is_alive_living_body_not_ghost(self):
        # A normal living body with positive HP stays alive, and a living body
        # with HP=0 stays dead — the ghost check must not perturb either path.
        s = SelfState(serial=0x01)
        s.body = 0x0190  # ordinary human male
        s.hits = 50
        s.hits_max = 100
        assert s.is_ghost is False
        assert s.is_alive is True
        s.hits = 0
        assert s.is_alive is False


# ---------------------------------------------------------------------------
# SocialState
# ---------------------------------------------------------------------------


class TestSocialState:
    def test_add_speech(self):
        ss = SocialState()
        entry = ss.add_speech(0x01, "Alice", "Hello!", 0, 0x0034)
        assert entry.name == "Alice"
        assert entry.text == "Hello!"
        assert len(ss.journal) == 1

    def test_journal_max_size(self):
        ss = SocialState()
        for i in range(150):
            ss.add_speech(0x01, "NPC", f"msg {i}", 0)
        assert len(ss.journal) == 100  # capped at MAX_JOURNAL_SIZE

    def test_recent(self):
        ss = SocialState()
        for i in range(20):
            ss.add_speech(0x01, "NPC", f"msg {i}", 0)
        recent = ss.recent(5)
        assert len(recent) == 5
        assert recent[-1].text == "msg 19"

    def test_search(self):
        ss = SocialState()
        ss.add_speech(0x01, "Alice", "I sell swords", 0)
        ss.add_speech(0x02, "Bob", "I buy shields", 0)
        ss.add_speech(0x03, "Charlie", "Nice sword!", 0)
        results = ss.search("sword")
        assert len(results) == 2


# ---------------------------------------------------------------------------
# EventStream
# ---------------------------------------------------------------------------


class TestEventStream:
    def test_emit_and_poll(self):
        es = EventStream()
        es.emit(GameEventType.MOBILE_APPEARED, {"serial": 1})
        es.emit(GameEventType.SPEECH_HEARD, {"text": "hi"})
        events = es.poll()
        assert len(events) == 2
        assert events[0].type == GameEventType.MOBILE_APPEARED
        assert events[1].type == GameEventType.SPEECH_HEARD

    def test_poll_clears(self):
        es = EventStream()
        es.emit(GameEventType.MOBILE_APPEARED)
        es.poll()
        assert es.poll() == []

    def test_peek_does_not_consume(self):
        es = EventStream()
        es.emit(GameEventType.HP_CHANGED, {"hp": 50})
        peeked = es.peek(1)
        assert len(peeked) == 1
        # Still available via poll
        events = es.poll()
        assert len(events) == 1

    def test_peek_zero_returns_empty(self):
        # peek(0) must mean "no events"; the naive events[-0:] slice
        # collapses to events[:] and would leak the whole ring buffer.
        es = EventStream()
        for i in range(5):
            es.emit(GameEventType.MOBILE_MOVED, {"i": i})
        assert es.peek(0) == []
        # negative counts are equally nonsensical and must not slice
        # from the wrong end of the buffer.
        assert es.peek(-1) == []
        # peek still does not consume: a normal peek and poll work after.
        assert len(es.peek(2)) == 2
        assert es.peek(2)[-1].data["i"] == 4
        assert len(es.poll()) == 5

    def test_pending_count(self):
        es = EventStream()
        assert es.pending_count == 0
        es.emit(GameEventType.MOBILE_APPEARED)
        es.emit(GameEventType.MOBILE_MOVED)
        assert es.pending_count == 2
        es.poll()
        assert es.pending_count == 0
