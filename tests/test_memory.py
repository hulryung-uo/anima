"""Tests for the persistent memory system."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from anima.memory.database import MemoryDB
from anima.memory.database import ActionStat
from anima.memory.retrieval import _format_action_stats
from anima.skills.state import region_coords


@pytest.fixture
async def db(tmp_path):
    """Create an in-memory-like temp database for testing."""
    memory = MemoryDB(tmp_path / "test.db")
    await memory.init()
    yield memory
    await memory.close()


# ---------------------------------------------------------------------------
# Episode tests
# ---------------------------------------------------------------------------


class TestEpisodes:
    @pytest.mark.asyncio
    async def test_record_and_query(self, db: MemoryDB) -> None:
        eid = await db.record_episode(
            agent_name="Anima",
            location_x=1000,
            location_y=2000,
            action="go",
            target="tavern",
            outcome="success",
            reward=10.0,
            summary="Arrived at tavern",
        )
        assert eid == 1

        episodes = await db.query_episodes("Anima", limit=5)
        assert len(episodes) == 1
        assert episodes[0].action == "go"
        assert episodes[0].target == "tavern"
        assert episodes[0].outcome == "success"
        assert episodes[0].reward == 10.0

    @pytest.mark.asyncio
    async def test_query_by_location(self, db: MemoryDB) -> None:
        await db.record_episode("Anima", 1000, 2000, "go", "tavern", "success")
        await db.record_episode("Anima", 5000, 6000, "go", "dock", "success")

        # Should find only the nearby episode
        nearby = await db.query_episodes("Anima", location_x=1010, location_y=2010, limit=5)
        assert len(nearby) == 1
        assert nearby[0].target == "tavern"

    @pytest.mark.asyncio
    async def test_query_by_action(self, db: MemoryDB) -> None:
        await db.record_episode("Anima", 1000, 2000, "go", "tavern", "success")
        await db.record_episode("Anima", 1000, 2000, "speak", "hello", "success")

        go_episodes = await db.query_episodes("Anima", action="go", limit=5)
        assert len(go_episodes) == 1
        assert go_episodes[0].action == "go"

    @pytest.mark.asyncio
    async def test_prune_episodes(self, db: MemoryDB) -> None:
        for i in range(10):
            await db.record_episode("Anima", 1000, 2000, "go", f"place_{i}", "success")

        pruned = await db.prune_episodes("Anima", max_count=5)
        assert pruned == 5
        assert await db.count_episodes("Anima") == 5

    @pytest.mark.asyncio
    async def test_prune_keeps_high_signal_episodes(self) -> None:
        """Pruning must evict the flattest (|reward|~0) episodes first and
        protect high-magnitude memories — a death (large negative reward) or a
        windfall (large positive reward) — regardless of age. Uses in-memory
        SQLite so the ordering, not a temp file, is what's under test."""
        mem = MemoryDB(":memory:")
        await mem.init()
        try:
            # A big-signal memory recorded FIRST (oldest): a near-death event.
            death = await mem.record_episode(
                "Anima", 0, 0, "melee_attack", "ettin", "death", reward=-20.0
            )
            # A big-signal windfall, also old.
            windfall = await mem.record_episode(
                "Anima", 0, 0, "loot", "corpse", "success", reward=15.0
            )
            # Then a flood of recent, zero-signal noise.
            for i in range(8):
                await mem.record_episode(
                    "Anima", 0, 0, "speak", f"hello_{i}", "neutral", reward=0.0
                )

            # 10 episodes; keep only 2.
            pruned = await mem.prune_episodes("Anima", max_count=2)
            assert pruned == 8
            assert await mem.count_episodes("Anima") == 2

            survivors = await mem.query_episodes("Anima", limit=10)
            survivor_ids = {ep.id for ep in survivors}
            # The two high-|reward| memories survive even though they are the
            # OLDEST rows; the recent zero-reward noise is what gets evicted.
            assert survivor_ids == {death, windfall}
            assert all(abs(ep.reward) > 0 for ep in survivors)
        finally:
            await mem.close()

    @pytest.mark.asyncio
    async def test_agent_name_isolation(self, db: MemoryDB) -> None:
        await db.record_episode("Anima", 1000, 2000, "go", "tavern", "success")
        await db.record_episode("Other", 1000, 2000, "go", "bank", "success")

        anima_eps = await db.query_episodes("Anima", limit=10)
        other_eps = await db.query_episodes("Other", limit=10)
        assert len(anima_eps) == 1
        assert len(other_eps) == 1
        assert anima_eps[0].target == "tavern"
        assert other_eps[0].target == "bank"


# ---------------------------------------------------------------------------
# Knowledge tests
# ---------------------------------------------------------------------------


class TestKnowledge:
    @pytest.mark.asyncio
    async def test_add_and_query(self, db: MemoryDB) -> None:
        kid = await db.add_knowledge("Anima", "The bank is at (1434, 1699)", "experience", 0.7)
        assert kid == 1

        facts = await db.query_knowledge("Anima", keyword="bank")
        assert len(facts) == 1
        assert "bank" in facts[0].fact
        assert facts[0].confidence == 0.7

    @pytest.mark.asyncio
    async def test_query_no_keyword(self, db: MemoryDB) -> None:
        await db.add_knowledge("Anima", "fact 1", "experience", 0.5)
        await db.add_knowledge("Anima", "fact 2", "experience", 0.8)

        facts = await db.query_knowledge("Anima")
        assert len(facts) == 2
        # Ordered by confidence desc
        assert facts[0].confidence == 0.8

    @pytest.mark.asyncio
    async def test_confirm_knowledge(self, db: MemoryDB) -> None:
        kid = await db.add_knowledge("Anima", "some fact", "experience", 0.5)
        await db.confirm_knowledge(kid)

        facts = await db.query_knowledge("Anima")
        assert facts[0].confidence == pytest.approx(0.6, abs=0.01)


# ---------------------------------------------------------------------------
# Relationship tests
# ---------------------------------------------------------------------------


class TestRelationships:
    @pytest.mark.asyncio
    async def test_create_relationship(self, db: MemoryDB) -> None:
        await db.update_relationship(
            "Anima", entity_serial=12345, entity_name="hulryung",
            disposition_delta=0.3, trust_delta=0.1, note="Friendly greeting",
        )
        rel = await db.get_relationship("Anima", 12345)
        assert rel is not None
        assert rel.entity_name == "hulryung"
        assert rel.disposition == pytest.approx(0.3)
        assert rel.trust == pytest.approx(0.6)  # 0.5 + 0.1
        assert rel.interaction_count == 1

    @pytest.mark.asyncio
    async def test_update_relationship(self, db: MemoryDB) -> None:
        await db.update_relationship("Anima", 12345, "hulryung", 0.2, 0.1)
        await db.update_relationship("Anima", 12345, "hulryung", 0.1, 0.05)

        rel = await db.get_relationship("Anima", 12345)
        assert rel is not None
        assert rel.disposition == pytest.approx(0.3)
        assert rel.trust == pytest.approx(0.65)
        assert rel.interaction_count == 2

    @pytest.mark.asyncio
    async def test_disposition_clamped(self, db: MemoryDB) -> None:
        await db.update_relationship("Anima", 12345, "hulryung", 0.9, 0.0)
        await db.update_relationship("Anima", 12345, "hulryung", 0.5, 0.0)

        rel = await db.get_relationship("Anima", 12345)
        assert rel is not None
        assert rel.disposition <= 1.0

    @pytest.mark.asyncio
    async def test_get_nearby_relationships(self, db: MemoryDB) -> None:
        await db.update_relationship("Anima", 100, "alice", 0.2, 0.1)
        await db.update_relationship("Anima", 200, "bob", -0.1, 0.0)
        await db.update_relationship("Anima", 300, "charlie", 0.5, 0.3)

        rels = await db.get_nearby_relationships("Anima", [100, 300])
        assert len(rels) == 2
        names = {r.entity_name for r in rels}
        assert names == {"alice", "charlie"}

    @pytest.mark.asyncio
    async def test_get_nearby_empty(self, db: MemoryDB) -> None:
        rels = await db.get_nearby_relationships("Anima", [])
        assert rels == []

    @pytest.mark.asyncio
    async def test_nearby_relationships_ranked_by_significance(self, db: MemoryDB) -> None:
        """The most significant relationships must surface first, not arbitrary
        rowid order: most interactions, then strongest feeling, then recency."""
        # Inserted in low->high significance order so rowid order would be wrong.
        # "stranger": met once, indifferent.
        await db.update_relationship("Anima", 1, "stranger", disposition_delta=0.05)
        # "rival": one tense brush, but a single interaction.
        await db.update_relationship("Anima", 2, "rival", disposition_delta=-0.6)
        # "regular": met many times.
        for _ in range(5):
            await db.update_relationship("Anima", 3, "regular", disposition_delta=0.02)

        rels = await db.get_nearby_relationships("Anima", [1, 2, 3])
        # "regular" (5 interactions) outranks the single-interaction rows; among
        # those, "rival" (|disp|=0.6) beats "stranger" (|disp|=0.05).
        assert [r.entity_name for r in rels] == ["regular", "rival", "stranger"]

    @pytest.mark.asyncio
    async def test_nearby_relationships_capped(self, db: MemoryDB) -> None:
        """A crowded area must not dump every known relationship into the LLM
        context — the result is capped, and the cap keeps the top-ranked ones."""
        serials = list(range(100, 120))  # 20 known entities nearby
        for s in serials:
            # interaction_count == s - 100, so higher serial == more significant.
            for _ in range(s - 100 + 1):
                await db.update_relationship("Anima", s, f"e{s}", disposition_delta=0.01)

        rels = await db.get_nearby_relationships("Anima", serials, limit=8)
        assert len(rels) == 8
        # The 8 kept are the most-interacted entities (serials 119..112), not a
        # rowid-order slice that would have started at serial 100.
        kept = {r.entity_serial for r in rels}
        assert kept == set(range(112, 120))


# ---------------------------------------------------------------------------
# Action stats tests
# ---------------------------------------------------------------------------


class TestActionStats:
    @pytest.mark.asyncio
    async def test_track_success(self, db: MemoryDB) -> None:
        await db.update_action_stats("Anima", "exploring", "go", success=True, reward=10.0)
        await db.update_action_stats("Anima", "exploring", "go", success=True, reward=5.0)
        await db.update_action_stats("Anima", "exploring", "go", success=False, reward=-5.0)

        stats = await db.get_action_stats("Anima", "exploring")
        assert len(stats) == 1
        assert stats[0].successes == 2
        assert stats[0].failures == 1
        assert stats[0].total_reward == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_separate_contexts(self, db: MemoryDB) -> None:
        await db.update_action_stats("Anima", "exploring", "go", True, 5.0)
        await db.update_action_stats("Anima", "near_player", "speak", True, 3.0)

        exploring = await db.get_action_stats("Anima", "exploring")
        near_player = await db.get_action_stats("Anima", "near_player")
        assert len(exploring) == 1
        assert len(near_player) == 1
        assert exploring[0].action == "go"
        assert near_player[0].action == "speak"

    @pytest.mark.asyncio
    async def test_get_all_stats(self, db: MemoryDB) -> None:
        await db.update_action_stats("Anima", "ctx1", "go", True, 5.0)
        await db.update_action_stats("Anima", "ctx2", "speak", True, 3.0)

        all_stats = await db.get_all_action_stats("Anima")
        assert len(all_stats) == 2


class TestActionStatFormatting:
    """The LLM context line ranks actions by the avg-reward it actually prints."""

    @staticmethod
    def _stat(action: str, successes: int, failures: int, total_reward: float) -> ActionStat:
        return ActionStat(
            id=0,
            agent_name="Anima",
            context_pattern="exploring",
            action=action,
            successes=successes,
            failures=failures,
            total_reward=total_reward,
            last_updated=0.0,
        )

    def _action_order(self, lines: list[str]) -> list[str]:
        # Each line looks like:  - "name": s/t success (avg reward: +x.x)
        return [ln.split('"')[1] for ln in lines]

    def test_ranked_by_average_not_total(self) -> None:
        # "grind": huge cumulative reward from sheer volume, mediocre per-try.
        #   100 tries * 0.5 = 50.0 total, avg = +0.5
        # "raid": tiny cumulative, but excellent per-try value.
        #   1 try * 20.0 = 20.0 total, avg = +20.0
        # get_action_stats returns them total-DESC -> grind first; the renderer
        # must flip them so the printed avg-reward order is honoured.
        stats = [
            self._stat("grind", successes=100, failures=0, total_reward=50.0),
            self._stat("raid", successes=1, failures=0, total_reward=20.0),
        ]
        lines = _format_action_stats(stats)
        assert self._action_order(lines) == ["raid", "grind"]
        # And the figure shown next to the top action is its true average.
        assert "+20.0" in lines[0]

    def test_skips_zero_attempt_rows(self) -> None:
        stats = [
            self._stat("ghost", successes=0, failures=0, total_reward=0.0),
            self._stat("go", successes=2, failures=0, total_reward=4.0),
        ]
        lines = _format_action_stats(stats)
        assert self._action_order(lines) == ["go"]

    def test_average_uses_successes_plus_failures(self) -> None:
        # 2 success + 2 failure = 4 attempts; total 4.0 -> avg +1.0 (not +2.0).
        stats = [self._stat("go", successes=2, failures=2, total_reward=4.0)]
        lines = _format_action_stats(stats)
        assert lines == ['  - "go": 2/4 success (avg reward: +1.0)']

    def test_block_is_capped_like_every_other_retrieval_channel(self) -> None:
        # One row per distinct action accumulates in a context bucket over a run.
        # The action-stats block is the only retrieval channel that was uncapped,
        # so a long-lived agent floods the LLM prompt with its full action ledger.
        # Ten distinct actions, each one try, with avg reward = its index so the
        # ranking (avg DESC) is unambiguous.
        stats = [
            self._stat(f"act{i}", successes=1, failures=0, total_reward=float(i))
            for i in range(10)
        ]
        lines = _format_action_stats(stats)
        # Default cap matches the q-values sibling ([:8]).
        assert len(lines) == 8
        actions = self._action_order(lines)
        # Highest-avg action surfaces first; the two lowest-avg are evicted.
        assert actions[0] == "act9"
        assert "act0" not in actions
        assert "act1" not in actions
        # Everything that survived is genuinely better than what was dropped.
        assert set(actions) == {f"act{i}" for i in range(2, 10)}

    def test_limit_is_overridable(self) -> None:
        stats = [
            self._stat(f"act{i}", successes=1, failures=0, total_reward=float(i))
            for i in range(5)
        ]
        lines = _format_action_stats(stats, limit=2)
        assert len(lines) == 2
        assert self._action_order(lines) == ["act4", "act3"]


# ---------------------------------------------------------------------------
# Location value tests
# ---------------------------------------------------------------------------


class TestLocationValues:
    @pytest.mark.asyncio
    async def test_ranked_by_average_not_total(self, db: MemoryDB) -> None:
        # "mine": many low-quality visits -> high cumulative, low average.
        for _ in range(10):
            await db.update_location_value("Anima", 5, 6, "mine", reward=1.0)
        # "hunt": few high-quality visits -> low cumulative, high average.
        for _ in range(2):
            await db.update_location_value("Anima", 5, 6, "hunt", reward=9.0)

        values = await db.get_location_values("Anima", 5, 6)
        assert [v[0] for v in values] == ["hunt", "mine"]

        # Sanity: cumulative total of "mine" (10.0) is not below "hunt" (18.0)
        # here, but average must drive the order regardless.
        hunt = next(v for v in values if v[0] == "hunt")
        mine = next(v for v in values if v[0] == "mine")
        assert hunt[1] / hunt[2] > mine[1] / mine[2]

    @pytest.mark.asyncio
    async def test_total_outranks_but_average_loses(self, db: MemoryDB) -> None:
        # "grind": huge cumulative reward from sheer volume, mediocre per-visit.
        for _ in range(100):
            await db.update_location_value("Anima", 0, 0, "grind", reward=0.5)
        # "raid": tiny cumulative, but excellent per-visit value.
        await db.update_location_value("Anima", 0, 0, "raid", reward=20.0)

        values = await db.get_location_values("Anima", 0, 0)
        # "grind" total = 50.0 >> "raid" total = 20.0, but "raid" avg = 20.0
        # >> "grind" avg = 0.5, so the genuinely better area must rank first.
        assert values[0][0] == "raid"

    @pytest.mark.asyncio
    async def test_accumulates_visits_and_reward(self, db: MemoryDB) -> None:
        await db.update_location_value("Anima", 1, 1, "fish", reward=3.0)
        await db.update_location_value("Anima", 1, 1, "fish", reward=5.0)

        values = await db.get_location_values("Anima", 1, 1)
        assert len(values) == 1
        activity, total, visits = values[0]
        assert activity == "fish"
        assert total == pytest.approx(8.0)
        assert visits == 2


# ---------------------------------------------------------------------------
# Relationship concurrency tests
# ---------------------------------------------------------------------------


class TestRelationshipConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_updates_no_duplicate_rows(self, db: MemoryDB) -> None:
        """Concurrent update_relationship calls for the same entity must not
        create duplicate rows or lose interaction-count/disposition updates.

        Before the fix, the read-then-INSERT path interleaved under
        asyncio.gather: both calls saw "no existing row" and each INSERTed,
        leaving two rows each with interaction_count == 1.
        """
        await asyncio.gather(
            db.update_relationship("Anima", 42, entity_name="Bob", disposition_delta=0.1),
            db.update_relationship("Anima", 42, entity_name="Bob", disposition_delta=0.1),
            db.update_relationship("Anima", 42, entity_name="Bob", disposition_delta=0.1),
        )

        rows = await db.db.execute_fetchall(
            "SELECT * FROM relationships WHERE agent_name = ? AND entity_serial = ?",
            ("Anima", 42),
        )
        assert len(rows) == 1, "duplicate relationship rows were created"

        rel = await db.get_relationship("Anima", 42)
        assert rel is not None
        # All three interactions were counted (no lost update).
        assert rel.interaction_count == 3
        # Disposition deltas accumulated (3 * 0.1), clamped to [-1, 1].
        assert rel.disposition == pytest.approx(0.3)
        assert rel.entity_name == "Bob"

    @pytest.mark.asyncio
    async def test_unique_index_present(self, db: MemoryDB) -> None:
        """The (agent_name, entity_serial) index must be UNIQUE so duplicate
        rows are structurally impossible even outside the in-process lock."""
        rows = await db.db.execute_fetchall("PRAGMA index_list('relationships')")
        unique_cols = {
            r["name"]: r["unique"] for r in rows
        }
        assert unique_cols.get("idx_relationships_agent") == 1


# ---------------------------------------------------------------------------
# Episode -> location-value wiring
# ---------------------------------------------------------------------------


def _fake_ctx(memory_db: MemoryDB, x: int, y: int, hp_percent: int = 100):
    """A minimal BrainContext stand-in for _record_episode.

    _record_episode reads: ctx.memory_db, ctx.blackboard (a real dict),
    ctx.perception.self_state.{x,y,hp_percent,serial}, ctx.llm,
    ctx.cfg.memory.max_episodes, and (via _infer_context_pattern ->
    has_player_nearby) ctx.perception.world.nearby_mobiles(...).
    """
    ctx = MagicMock()
    ctx.memory_db = memory_db
    ctx.llm = None  # skip the reflection branch
    ctx.blackboard = {}  # must be a real dict (get/set used)
    ctx.cfg.memory.max_episodes = 1000
    ss = ctx.perception.self_state
    ss.x = x
    ss.y = y
    ss.hp_percent = hp_percent
    ss.serial = 1
    ctx.perception.world.nearby_mobiles.return_value = []  # nobody nearby
    return ctx


class TestEpisodeLocationValueWiring:
    @pytest.mark.asyncio
    async def test_rewarded_episode_populates_location_value(self, db: MemoryDB) -> None:
        """A rewarded episode must be tallied into the location-value map that
        retrieve_context reads back — previously update_location_value had no
        runtime caller and the 'best area' channel was always empty."""
        from anima.brain.think import _record_episode

        x, y = 1600, 1700
        ctx = _fake_ctx(db, x, y)
        await _record_episode(ctx, "mine_ore", "vein", "success", reward=7.0)

        rx, ry = region_coords(x, y)
        values = await db.get_location_values("Anima", rx, ry)
        assert len(values) == 1
        activity, total, visits = values[0]
        assert activity == "mine_ore"
        assert total == pytest.approx(7.0)
        assert visits == 1

    @pytest.mark.asyncio
    async def test_repeat_episodes_accumulate_in_region(self, db: MemoryDB) -> None:
        """Two rewarded episodes of the same activity in the same region
        accumulate visit_count and total_reward (per-visit average semantics)."""
        from anima.brain.think import _record_episode

        x, y = 1600, 1700
        ctx = _fake_ctx(db, x, y)
        await _record_episode(ctx, "mine_ore", "vein", "success", reward=4.0)
        await _record_episode(ctx, "mine_ore", "vein", "success", reward=6.0)

        rx, ry = region_coords(x, y)
        values = await db.get_location_values("Anima", rx, ry)
        assert len(values) == 1
        _, total, visits = values[0]
        assert total == pytest.approx(10.0)
        assert visits == 2

    @pytest.mark.asyncio
    async def test_zero_reward_episode_is_not_tallied(self, db: MemoryDB) -> None:
        """Zero-signal episodes carry no learning value and must not dilute the
        per-visit average the retrieval block ranks regions by."""
        from anima.brain.think import _record_episode

        x, y = 1600, 1700
        ctx = _fake_ctx(db, x, y)
        await _record_episode(ctx, "speak", "hello", "success", reward=0.0)

        rx, ry = region_coords(x, y)
        values = await db.get_location_values("Anima", rx, ry)
        assert values == []


# ---------------------------------------------------------------------------
# retrieve_context: location-value block framing
# ---------------------------------------------------------------------------


def _retrieval_ctx(memory_db: MemoryDB, x: int, y: int):
    """A minimal BrainContext stand-in for retrieve_context.

    retrieve_context reads ctx.memory_db, ctx.blackboard (persona lookup),
    ctx.perception.self_state.{x,y,hp_percent,serial,equipment,weight,
    weight_max}, and ctx.perception.world.{nearby_mobiles,nearby_items,items}.
    Empty world + no backpack short-circuits steps 1-5 so the location block
    (step 6) is exercised in isolation against the real db.
    """
    ctx = MagicMock()
    ctx.memory_db = memory_db
    ctx.blackboard = {}  # no persona -> agent name defaults to "Anima"
    ss = ctx.perception.self_state
    ss.x = x
    ss.y = y
    ss.hp_percent = 100
    ss.serial = 1
    ss.equipment = {}  # no backpack -> _inventory_state short-circuits
    ss.weight = 0
    ss.weight_max = 0
    ctx.perception.world.nearby_mobiles.return_value = []
    ctx.perception.world.nearby_items.return_value = []
    ctx.perception.world.items = {}
    return ctx


class TestRetrievalLocationFraming:
    @pytest.mark.asyncio
    async def test_negative_average_activity_is_a_warning_not_advice(
        self, db: MemoryDB
    ) -> None:
        """A region where an activity only ever lost reward must be framed as a
        cost to avoid, never under a "pays off" recommendation header."""
        from anima.memory.retrieval import retrieve_context

        x, y = 1600, 1700
        # "fight": repeatedly net-negative (the agent kept dying here).
        for _ in range(3):
            await db.update_location_value("Anima", *region_coords(x, y), "fight", -5.0)

        block = await retrieve_context(_retrieval_ctx(db, x, y))

        assert "fight" in block
        # The costly activity must appear under the avoid/costly header, not
        # under a pays-off recommendation.
        assert "costly" in block.lower() or "avoid" in block.lower()
        assert "pays off" not in block

    @pytest.mark.asyncio
    async def test_positive_and_negative_activities_are_separated(
        self, db: MemoryDB
    ) -> None:
        """Profitable and money-losing activities in the same region land in
        distinct sections so the LLM gets a clear directional signal."""
        from anima.memory.retrieval import retrieve_context

        x, y = 1600, 1700
        rx, ry = region_coords(x, y)
        await db.update_location_value("Anima", rx, ry, "mine", 8.0)   # pays off
        await db.update_location_value("Anima", rx, ry, "fight", -6.0)  # costly

        block = await retrieve_context(_retrieval_ctx(db, x, y))

        assert "pays off" in block
        assert "costly" in block.lower() or "avoid" in block.lower()

        # "mine" must sit in the pays-off section and "fight" in the avoid one,
        # never the other way round.
        pays_idx = block.index("pays off")
        avoid_anchor = "costly" if "costly" in block else "avoid"
        avoid_idx = block.index(avoid_anchor)
        mine_idx = block.index("mine")
        fight_idx = block.index("fight")
        # mine appears in the pays-off block (before the avoid header);
        # fight appears in the avoid block (after it).
        assert pays_idx < mine_idx < avoid_idx < fight_idx

    @pytest.mark.asyncio
    async def test_costly_warning_survives_many_profitable_activities(
        self, db: MemoryDB
    ) -> None:
        """A costly activity must still surface as a warning even when the region
        has more than five profitable activities.

        get_location_values orders by average reward DESC, so the negative-average
        ("avoid") rows sort last. A naive loc_values[:5] pre-slice would drop them
        once five higher-paying activities exist, silently suppressing the warning
        channel the block promises to preserve.
        """
        from anima.memory.retrieval import retrieve_context

        x, y = 1600, 1700
        rx, ry = region_coords(x, y)
        # Six profitable activities (all rank above the loss on avg-DESC) ...
        for i, reward in enumerate((9.0, 8.0, 7.0, 6.0, 5.0, 4.0)):
            await db.update_location_value("Anima", rx, ry, f"profit{i}", reward)
        # ... plus one activity that only ever lost reward.
        await db.update_location_value("Anima", rx, ry, "deathtrap", -8.0)

        block = await retrieve_context(_retrieval_ctx(db, x, y))

        # The warning must not be crowded out by the profitable activities.
        assert "deathtrap" in block
        assert "costly" in block.lower() or "avoid" in block.lower()
        avoid_anchor = "costly" if "costly" in block else "avoid"
        assert block.index(avoid_anchor) < block.index("deathtrap")

    @pytest.mark.asyncio
    async def test_avoid_section_keeps_the_worst_losses_not_the_mildest(self) -> None:
        """When a region has more than five net-negative activities, the avoid
        warning must surface the *most* costly ones (lowest average reward), not
        the mildest.

        get_location_values returns rows ordered by average reward DESC, so the
        negatives arrive least-costly first. Capping the avoid section by keeping
        the first five negatives encountered would retain the mildest losses and
        drop the genuinely lethal regions — the opposite of a warning's purpose.
        """
        from anima.memory.retrieval import retrieve_context

        # Use a real in-memory SQLite store so the SQL ordering is exercised.
        db = MemoryDB(":memory:")
        await db.init()
        try:
            x, y = 1600, 1700
            rx, ry = region_coords(x, y)
            # Six net-negative activities of escalating cost. The two deadliest
            # (-20, -18) must survive the five-row cap; the mildest (-2) must drop.
            costs = {
                "stub_toe": -2.0,
                "lose_gold": -5.0,
                "get_poisoned": -9.0,
                "ambushed": -12.0,
                "near_death": -18.0,
                "deathtrap": -20.0,
            }
            for activity, reward in costs.items():
                await db.update_location_value("Anima", rx, ry, activity, reward)

            block = await retrieve_context(_retrieval_ctx(db, x, y))

            avoid_anchor = "costly" if "costly" in block else "avoid"
            avoid_block = block[block.index(avoid_anchor):]

            # The deadliest activities must appear in the warning ...
            assert "deathtrap" in avoid_block
            assert "near_death" in avoid_block
            # ... and the mildest loss must have been evicted by the cap, not the
            # lethal ones.
            assert "stub_toe" not in avoid_block
            # Worst-first ordering: the deadliest appears before a milder loss.
            assert avoid_block.index("deathtrap") < avoid_block.index("ambushed")
        finally:
            await db.close()
