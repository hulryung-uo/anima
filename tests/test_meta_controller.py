"""Tests for the meta-controller (P0 shadow stage).

P0 invariant: the controller observes + logs which mode it WOULD pick, but
never changes anything the planner consumes. These tests pin that neutrality,
the LLM plumbing (mirrors StrategySelector), and graceful degradation.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from anima.perception.enums import NotorietyFlag
from anima.planner.modes import MODES, default_mode_for_profession
from anima.planner.meta_controller import (
    HeuristicModePolicy,
    LivingState,
    LlmModePolicy,
    MetaController,
    ModeDecision,
    build_living_state,
)


class TestModeMapping:
    def test_all_profession_loops_have_modes(self):
        # every profession the planner knows maps to a real mode
        for prof, mode in [("mage", "magery"), ("bard", "bard"),
                           ("thief", "thief"), ("adventurer", "combat"),
                           ("blacksmith", "smithing"), ("miner", "mining"),
                           ("", "mining")]:
            assert default_mode_for_profession(prof) == mode
            assert mode in MODES

    def test_unknown_profession_defaults_to_mining(self):
        assert default_mode_for_profession("wizard_of_oz") == "mining"


def _make_state(actual_mode="mining", **kw):
    base = dict(hp_frac=0.8, weight_frac=0.2, gold=100, gold_rate_per_min=2.0,
                nearby_mobiles=0, danger_nearby=False, inventory_text="(empty)",
                session_minutes=10.0, phase="early", last_modes=[],
                actual_mode=actual_mode)
    base.update(kw)
    return LivingState(**base)


class TestLlmModePolicyParsing:
    @pytest.mark.asyncio
    async def test_valid_response_parsed(self):
        llm = MagicMock()
        llm.chat = AsyncMock(return_value=MagicMock(
            text="MODE: smithing\nGOAL: make 5 daggers\nREASONING: convert ore to gold"))
        d = await LlmModePolicy().choose_with_llm(_make_state(), llm)
        assert d.mode == "smithing"
        assert d.goal == "make 5 daggers"
        assert "gold" in d.rationale

    @pytest.mark.asyncio
    async def test_goal_none_normalized(self):
        llm = MagicMock()
        llm.chat = AsyncMock(return_value=MagicMock(
            text="MODE: rest\nGOAL: none\nREASONING: low hp"))
        d = await LlmModePolicy().choose_with_llm(_make_state(), llm)
        assert d.mode == "rest"
        assert d.goal is None

    @pytest.mark.asyncio
    async def test_unknown_mode_keeps_actual(self):
        llm = MagicMock()
        llm.chat = AsyncMock(return_value=MagicMock(text="MODE: teleporting\n..."))
        d = await LlmModePolicy().choose_with_llm(_make_state(actual_mode="magery"), llm)
        assert d.mode == "magery"  # fell back to actual, didn't invent

    @pytest.mark.asyncio
    async def test_garbage_keeps_actual(self):
        llm = MagicMock()
        llm.chat = AsyncMock(return_value=MagicMock(text="i am a teapot"))
        d = await LlmModePolicy().choose_with_llm(_make_state(actual_mode="bard"), llm)
        assert d.mode == "bard"


class TestHeuristicModePolicy:
    @pytest.mark.asyncio
    async def test_danger_returns_rest(self):
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="mining", danger_nearby=True))
        assert d.mode == "rest"
        assert d.mode in MODES
        assert d.goal is None

    @pytest.mark.asyncio
    async def test_low_hp_returns_rest(self):
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="combat", hp_frac=0.4))
        assert d.mode == "rest"
        assert d.goal is None

    @pytest.mark.asyncio
    async def test_overweight_returns_travel(self):
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="mining", weight_frac=0.95,
                        hp_frac=0.9, danger_nearby=False))
        assert d.mode == "travel"
        assert d.mode in MODES
        assert d.goal is None

    @pytest.mark.asyncio
    async def test_flat_gold_rate_relocates_a_gold_mode(self):
        # A gold-earning mode (mining) past the early phase with a non-trivial
        # pack and a flat/negative gold rate is a dead loop → travel to
        # relocate/offload (docs §5 economic-sustainability lever), NOT keep
        # grinding ground that no longer pays.
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="mining", hp_frac=0.9, weight_frac=0.5,
                        danger_nearby=False, phase="mid",
                        gold_rate_per_min=0.0,
                        last_modes=["mining", "combat"]))
        assert d.mode == "travel"
        assert d.mode in MODES
        assert d.goal is None
        assert "economy" in d.rationale

    @pytest.mark.asyncio
    async def test_negative_gold_rate_relocates(self):
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="smithing", hp_frac=0.9, weight_frac=0.4,
                        danger_nearby=False, phase="late",
                        gold_rate_per_min=-3.0,
                        last_modes=["smithing", "smithing"]))
        assert d.mode == "travel"
        assert d.goal is None

    @pytest.mark.asyncio
    async def test_positive_gold_rate_does_not_relocate(self):
        # Healthy gold flow → keep working; the economy branch must not fire.
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="mining", hp_frac=0.9, weight_frac=0.5,
                        danger_nearby=False, phase="mid",
                        gold_rate_per_min=4.0,
                        last_modes=["mining", "combat"]))
        assert d.mode == "mining"   # day-phase default keeps grinding
        assert d.goal is None

    @pytest.mark.asyncio
    async def test_flat_rate_in_skill_grind_does_not_relocate(self):
        # Pure skill-grinds (magery) earn no gold by design — a flat gold rate
        # there is expected and must NOT pull the avatar off its skill stint.
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="magery", hp_frac=0.9, weight_frac=0.5,
                        danger_nearby=False, phase="mid",
                        gold_rate_per_min=0.0,
                        last_modes=["magery", "combat"]))
        assert d.mode == "magery"   # not travel
        assert d.goal is None

    @pytest.mark.asyncio
    async def test_flat_rate_early_phase_does_not_relocate(self):
        # Early phase = too few gold samples to trust a 0.0 rate; keep grinding.
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="mining", hp_frac=0.9, weight_frac=0.5,
                        danger_nearby=False, phase="early",
                        gold_rate_per_min=0.0,
                        last_modes=["mining", "combat"]))
        assert d.mode == "mining"
        assert d.goal is None

    @pytest.mark.asyncio
    async def test_flat_rate_empty_pack_does_not_relocate(self):
        # Nothing to offload (light pack) → a relocation trip is pointless;
        # the weight gate keeps the avatar grinding instead of wandering.
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="mining", hp_frac=0.9, weight_frac=0.05,
                        danger_nearby=False, phase="mid",
                        gold_rate_per_min=0.0,
                        last_modes=["mining", "combat"]))
        assert d.mode == "mining"
        assert d.goal is None

    @pytest.mark.asyncio
    async def test_balance_rotates_off_actual(self):
        # last 3 stints all the actual mode, healthy/safe → rotate to a
        # different low-risk productive mode.
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="mining", hp_frac=0.9, weight_frac=0.2,
                        danger_nearby=False, phase="early",
                        last_modes=["mining", "mining", "mining"]))
        assert d.mode != "mining"
        assert d.mode in MODES
        assert d.goal is None

    @pytest.mark.asyncio
    async def test_balance_rotation_is_deterministic(self):
        st = _make_state(actual_mode="mining", hp_frac=0.9, weight_frac=0.2,
                         danger_nearby=False, phase="early",
                         last_modes=["mining", "mining", "mining"])
        a = await HeuristicModePolicy().choose(st)
        b = await HeuristicModePolicy().choose(st)
        assert a.mode == b.mode  # pure / no randomness

    @pytest.mark.asyncio
    async def test_mid_phase_heavy_pack_travels_to_offload(self):
        # docs/meta-controller.md §5 principle 5: mid=economy. A heavy-but-not-
        # overweight pack in the mid phase must steer to travel (sell/offload),
        # NOT keep grinding like the early phase. Previously mid mapped to the
        # actual mode, so this economy routine never fired.
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="mining", hp_frac=0.9, weight_frac=0.8,
                        danger_nearby=False, phase="mid",
                        gold_rate_per_min=3.0,  # profitable: branch (c) won't fire
                        last_modes=["mining", "combat"]))
        assert d.mode == "travel"
        assert d.mode in MODES
        assert d.goal is None
        assert "economy" in d.rationale

    @pytest.mark.asyncio
    async def test_mid_phase_skill_grinder_heavy_pack_offloads(self):
        # Pure skill-grinders (magery) are excluded from the branch (c) economy
        # lever (flat gold rate is expected), so the mid day-phase routine is
        # the ONLY way they offload a full pack. It must fire here.
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="magery", hp_frac=0.9, weight_frac=0.85,
                        danger_nearby=False, phase="mid",
                        gold_rate_per_min=0.0,
                        last_modes=["magery", "combat"]))
        assert d.mode == "travel"
        assert d.goal is None

    @pytest.mark.asyncio
    async def test_mid_phase_light_pack_keeps_grinding(self):
        # Below the economy weight threshold the mid phase stays productive —
        # the existing comfortable-grind behaviour is preserved.
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="mining", hp_frac=0.9, weight_frac=0.5,
                        danger_nearby=False, phase="mid",
                        gold_rate_per_min=3.0,
                        last_modes=["mining", "combat"]))
        assert d.mode == "mining"
        assert d.goal is None

    @pytest.mark.asyncio
    async def test_early_phase_heavy_pack_still_grinds(self):
        # The economy routine is mid-only; an early-phase heavy pack keeps
        # grinding (economy is a mid-phase concern per the day-phase mapping).
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="mining", hp_frac=0.9, weight_frac=0.8,
                        danger_nearby=False, phase="early",
                        gold_rate_per_min=3.0,
                        last_modes=["mining", "combat"]))
        assert d.mode == "mining"
        assert d.goal is None

    @pytest.mark.asyncio
    async def test_late_phase_socializes(self):
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="mining", hp_frac=0.9, weight_frac=0.2,
                        danger_nearby=False, phase="late",
                        last_modes=["mining", "combat"]))
        assert d.mode == "socialize"
        assert d.goal is None

    @pytest.mark.asyncio
    async def test_early_phase_keeps_actual(self):
        d = await HeuristicModePolicy().choose(
            _make_state(actual_mode="magery", hp_frac=0.9, weight_frac=0.2,
                        danger_nearby=False, phase="early",
                        last_modes=["magery", "combat"]))
        assert d.mode == "magery"
        assert d.goal is None

    @pytest.mark.asyncio
    async def test_every_branch_returns_valid_mode_with_none_goal(self):
        policy = HeuristicModePolicy()
        cases = [
            _make_state(danger_nearby=True),
            _make_state(hp_frac=0.1),
            _make_state(weight_frac=0.99, hp_frac=0.9, danger_nearby=False),
            _make_state(actual_mode="mining", hp_frac=0.9, danger_nearby=False,
                        last_modes=["mining", "mining", "mining"]),
            _make_state(phase="late", hp_frac=0.9, danger_nearby=False),
            _make_state(phase="mid", hp_frac=0.9, danger_nearby=False),
            # actual_mode not in MODES → clamps to mining
            _make_state(actual_mode="bogus", hp_frac=0.9, danger_nearby=False,
                        phase="mid"),
        ]
        for st in cases:
            d = await policy.choose(st)
            assert d.mode in MODES
            assert d.goal is None


class TestMetaControllerShadow:
    def test_min_interval_enforced(self):
        assert MetaController(interval_s=5).interval_s >= 60.0

    def test_active_mode_none_before_first_decision(self):
        assert MetaController().active_mode is None

    @pytest.mark.asyncio
    async def test_no_llm_uses_heuristic_and_logs(self, tmp_path, monkeypatch):
        # P0 shadow stage must still log during LLM-less (deterministic eval)
        # runs — the heuristic fallback produces a decision and a JSONL row.
        import anima.planner.meta_controller as mod
        monkeypatch.setattr(mod, "_SHADOW_LOG", tmp_path / "meta_shadow.jsonl")
        c = MetaController()
        ctx = MagicMock()
        ctx.llm = None
        ctx.persona.profession = "miner"
        ss = ctx.perception.self_state
        ss.hits = 80; ss.hits_max = 100; ss.weight = 50; ss.weight_max = 400
        ss.gold = 100; ss.x = 10; ss.y = 20; ss.serial = 0x1
        ss.equipment = {0x15: 0x101}
        ctx.blackboard = {}  # real dict — _net_worth reads the bank_balance cache
        ctx.perception.world.items = {}
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[])

        spawned = await c.maybe_decide(ctx)
        await c._decide_task
        assert spawned is True
        # heuristic now produces a decision (intended behaviour change)
        assert c.active_mode is not None
        assert c.active_mode in MODES
        log = (tmp_path / "meta_shadow.jsonl").read_text().strip()
        assert '"policy": "heuristic"' in log

    @pytest.mark.asyncio
    async def test_interval_gate_blocks_second_call(self):
        c = MetaController(interval_s=60)
        c._last_decide = time.monotonic()
        ctx = MagicMock()
        ctx.llm = None
        assert await c.maybe_decide(ctx) is False

    @pytest.mark.asyncio
    async def test_llm_decision_recorded(self, tmp_path, monkeypatch):
        import anima.planner.meta_controller as mod
        monkeypatch.setattr(mod, "_SHADOW_LOG", tmp_path / "meta_shadow.jsonl")
        c = MetaController()
        ctx = MagicMock()
        ctx.llm = MagicMock()
        ctx.llm.chat = AsyncMock(return_value=MagicMock(
            text="MODE: combat\nGOAL: hunt 3 rats\nREASONING: build presence"))
        ctx.persona.profession = "miner"
        ss = ctx.perception.self_state
        ss.hits = 80; ss.hits_max = 100; ss.weight = 50; ss.weight_max = 400
        ss.gold = 100; ss.x = 10; ss.y = 20; ss.serial = 0x1
        ss.equipment = {0x15: 0x101}
        ctx.blackboard = {}  # real dict — _net_worth reads the bank_balance cache
        ctx.perception.world.items = {}
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[])

        await c.maybe_decide(ctx)
        await c._decide_task
        assert c.active_mode == "combat"
        # shadow log written, with the comparison to the actual (miner→mining)
        log = (tmp_path / "meta_shadow.jsonl").read_text().strip()
        assert '"would_pick": "combat"' in log
        assert '"actual_mode": "mining"' in log
        assert '"agree": false' in log

    @pytest.mark.asyncio
    async def test_llm_exception_does_not_crash(self):
        c = MetaController()
        ctx = MagicMock()
        ctx.llm = MagicMock()
        ctx.llm.chat = AsyncMock(side_effect=RuntimeError("boom"))
        ctx.persona.profession = "mage"
        ss = ctx.perception.self_state
        ss.hits = 100; ss.hits_max = 100; ss.weight = 0; ss.weight_max = 400
        ss.gold = 0; ss.x = 0; ss.y = 0; ss.serial = 0x1
        ss.equipment = {0x15: 0x101}
        ctx.blackboard = {}  # real dict — _net_worth reads the bank_balance cache
        ctx.perception.world.items = {}
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[])
        await c.maybe_decide(ctx)
        await c._decide_task          # must not raise
        assert c.active_mode is None  # decision discarded

    @pytest.mark.asyncio
    async def test_maybe_decide_does_not_block_on_slow_llm(self):
        c = MetaController()
        gate = asyncio.Event()

        async def slow_chat(_msgs):
            await gate.wait()
            return MagicMock(text="MODE: mining\nREASONING: x")

        ctx = MagicMock()
        ctx.llm = MagicMock()
        ctx.llm.chat = slow_chat
        ctx.persona.profession = "miner"
        ss = ctx.perception.self_state
        ss.hits = 100; ss.hits_max = 100; ss.weight = 0; ss.weight_max = 400
        ss.gold = 0; ss.x = 0; ss.y = 0; ss.serial = 0x1
        ss.equipment = {0x15: 0x101}
        ctx.blackboard = {}  # real dict — _net_worth reads the bank_balance cache
        ctx.perception.world.items = {}
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[])

        spawned = await asyncio.wait_for(c.maybe_decide(ctx), timeout=0.5)
        assert spawned is True
        assert not c._decide_task.done()        # still running
        assert await asyncio.wait_for(c.maybe_decide(ctx), timeout=0.5) is False  # single-flight
        c._decide_task.cancel()
        try:
            await c._decide_task
        except BaseException:
            pass


class TestLivingStateBuilder:
    def test_build_living_state_phase_and_danger(self):
        ctx = MagicMock()
        ctx.persona.profession = "adventurer"
        ss = ctx.perception.self_state
        ss.hits = 30; ss.hits_max = 100; ss.weight = 200; ss.weight_max = 400
        ss.gold = 50; ss.x = 5; ss.y = 5; ss.serial = 0x1
        ss.equipment = {0x15: 0x101}
        ctx.perception.world.items = {}
        # A real live foe (red/murderer, non-human body, alive, not blessed).
        foe = SimpleNamespace(
            serial=0x2, notoriety=NotorietyFlag.MURDERER,
            body=0x0021, is_dead=False, is_yellow_health=False,
        )
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[foe])

        st = build_living_state(ctx, gold_rate_per_min=0.0,
                                session_minutes=60.0, last_modes=["combat"])
        assert st.actual_mode == "combat"     # adventurer → combat
        assert st.phase == "late"             # 60 min
        assert st.nearby_mobiles == 1
        assert st.danger_nearby is True        # 30% hp + foe nearby
        assert st.hp_frac == pytest.approx(0.3)

    def test_danger_proxy_excludes_friendly_and_invulnerable_mobiles(self):
        """A wounded avatar surrounded only by NON-foes is NOT "in danger".

        Regression guard for the friend/foe asymmetry: ``danger_nearby`` used
        to count EVERY nearby mobile, so a wounded warrior standing next to the
        co-located survival-arena Healer (invulnerable / yellow-bar) plus a
        friendly innocent NPC read as danger and steered the controller to
        ``rest`` away from a fight it had already won. The proxy must reuse the
        planner's ``_is_live_hostile`` filter — the same population the flee
        gate runs from — so friendlies, corpses and invulnerable Healers don't
        inflate the count.
        """
        ctx = MagicMock()
        ctx.persona.profession = "adventurer"
        ss = ctx.perception.self_state
        ss.hits = 30; ss.hits_max = 100; ss.weight = 200; ss.weight_max = 400
        ss.gold = 50; ss.x = 5; ss.y = 5; ss.serial = 0x1
        ss.equipment = {0x15: 0x101}
        ctx.perception.world.items = {}
        innocent_npc = SimpleNamespace(
            serial=0x2, notoriety=NotorietyFlag.INNOCENT,
            body=0x0190, is_dead=False, is_yellow_health=False,
        )
        # Invulnerable Healer (yellow health bar) — never a foe.
        healer = SimpleNamespace(
            serial=0x3, notoriety=NotorietyFlag.MURDERER,
            body=0x0190, is_dead=False, is_yellow_health=True,
        )
        # A felled mob still lingering in world.mobiles as a corpse.
        corpse = SimpleNamespace(
            serial=0x4, notoriety=NotorietyFlag.MURDERER,
            body=0x0021, is_dead=True, is_yellow_health=False,
        )
        ctx.perception.world.nearby_mobiles = MagicMock(
            return_value=[innocent_npc, healer, corpse]
        )

        st = build_living_state(ctx, gold_rate_per_min=0.0,
                                session_minutes=60.0, last_modes=["combat"])
        assert st.nearby_mobiles == 0          # none are live foes
        assert st.danger_nearby is False        # wounded but not in danger

    def test_unknown_hp_reads_as_full_not_zero(self):
        """Before the first status packet (hits_max==0) HP is UNKNOWN, not 0.

        Regression guard for a perception<->meta-controller default mismatch:
        every other layer treats ``hits_max == 0`` as "HP unknown → full/safe"
        (SelfState.hp_percent returns 100.0; combat_loop/planner gate HP tests
        behind ``hits_max > 0``). ``build_living_state`` used ``hits_max or 1``
        and ``ss.hits / hp_max``, so a freshly-spawned healthy avatar read as
        hp_frac=0/1=0.0 — tripping the heuristic policy's ``hp_frac < 0.5``
        survival-defer on its very first decision, and ``danger_nearby`` if any
        foe was near. Unknown HP must read as full (1.0), never 0.0.
        """
        ctx = MagicMock()
        ctx.persona.profession = "adventurer"
        ss = ctx.perception.self_state
        # No 0x11/0x16 status packet yet: both default to 0.
        ss.hits = 0; ss.hits_max = 0; ss.weight = 0; ss.weight_max = 0
        ss.gold = 0; ss.x = 5; ss.y = 5; ss.serial = 0x1
        ss.equipment = {0x15: 0x101}
        ctx.perception.world.items = {}
        foe = SimpleNamespace(
            serial=0x2, notoriety=NotorietyFlag.MURDERER,
            body=0x0021, is_dead=False, is_yellow_health=False,
        )
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[foe])

        st = build_living_state(ctx, gold_rate_per_min=0.0,
                                session_minutes=5.0, last_modes=["combat"])
        assert st.hp_frac == pytest.approx(1.0)  # unknown HP → full, not 0.0
        assert st.danger_nearby is False         # unknown HP is never "wounded"

        # And the heuristic policy must NOT fire a spurious survival-defer.
        import asyncio
        d = asyncio.run(HeuristicModePolicy().choose(st))
        assert d.mode != "rest"
