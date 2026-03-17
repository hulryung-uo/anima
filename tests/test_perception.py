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

    def test_pending_count(self):
        es = EventStream()
        assert es.pending_count == 0
        es.emit(GameEventType.MOBILE_APPEARED)
        es.emit(GameEventType.MOBILE_MOVED)
        assert es.pending_count == 2
        es.poll()
        assert es.pending_count == 0
