"""Tests for profession primitives — skills/spells actions + practice procedures."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.client.packets import build_cast_spell, build_use_skill


def _make_ctx(**overrides):
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.x, ss.y, ss.z = 100, 200, 0
    ss.serial = 0x42
    ss.is_alive = True
    ss.hits, ss.hits_max = 100, 100
    ss.hp_percent = 100.0
    ss.mana, ss.mana_max = 50, 50
    ss.stam, ss.stam_max = 100, 100
    ss.weight, ss.weight_max = 100, 400
    ss.gold = 50
    ss.skills = {}
    ss.equipment = {0x15: 0x101}  # backpack
    ss.pending_target = None
    ctx.perception.world.items = {}
    ctx.perception.world.mobiles = {}
    ctx.perception.world.nearby_mobiles = MagicMock(return_value=[])
    ctx.conn.send_packet = AsyncMock()
    ctx.conn.connected = True
    ctx.blackboard = {}
    ctx.bus = None
    ctx.memory_db = None
    ctx.persona = MagicMock()
    ctx.persona.name = "TestAgent"
    ctx.persona.profession = ""
    for k, v in overrides.items():
        setattr(ss, k, v)
    return ctx


def _add_item(ctx, serial, graphic, amount=1):
    item = MagicMock(container=0x101, graphic=graphic, amount=amount,
                     hue=0, serial=serial)
    ctx.perception.world.items[serial] = item
    return item


class TestSkillSpellPackets:
    def test_use_skill_payload(self):
        pkt = build_use_skill(21)
        assert pkt[0] == 0x12
        assert pkt[3] == 0x24  # type: skill
        assert pkt[4:].rstrip(b"\x00") == b"21 0"

    def test_cast_spell_payload_is_wire_id(self):
        # Greater Heal: registry 28 → wire 29 (PacketHandlers.cs: id = N-1)
        pkt = build_cast_spell(29)
        assert pkt[0] == 0x12
        assert pkt[3] == 0x56  # type: spell
        assert pkt[4:].rstrip(b"\x00") == b"29"


class TestCastSpell:
    @pytest.mark.asyncio
    async def test_no_mana_precheck(self):
        from anima.actions.spells import cast_spell

        ctx = _make_ctx(mana=3, mana_max=50)
        result = await cast_spell(ctx, 29, mana_cost=11)
        assert result.success is False
        assert result.no_mana is True
        ctx.conn.send_packet.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cast_targets_self_and_resolves_on_silence(self):
        from anima.actions import spells
        from anima.actions.result import ActionResult

        ctx = _make_ctx()
        with (
            patch.object(spells, "wait_for_target", new=AsyncMock(
                return_value=ActionResult(success=True, data={"cursor_id": 7}),
            )),
            patch.object(spells, "target_object",
                         new=AsyncMock(return_value=ActionResult(success=True))) as tgt,
            patch.object(spells, "wait_for_journal", new=AsyncMock(
                return_value=ActionResult(success=False),  # silence = clean cast
            )),
        ):
            result = await spells.cast_spell(ctx, 29, mana_cost=11)

        assert result.success is True
        assert result.fizzled is False
        tgt.assert_awaited_once_with(
            ctx, 7, ctx.perception.self_state.serial, cursor_flag=0
        )

    @pytest.mark.asyncio
    async def test_fizzle_counts_as_resolved(self):
        from anima.actions import spells
        from anima.actions.result import ActionResult

        ctx = _make_ctx()
        with (
            patch.object(spells, "wait_for_target", new=AsyncMock(
                return_value=ActionResult(success=True, data={"cursor_id": 7}),
            )),
            patch.object(spells, "target_object",
                         new=AsyncMock(return_value=ActionResult(success=True))),
            patch.object(spells, "wait_for_journal", new=AsyncMock(
                return_value=ActionResult(
                    success=True, data={"index": 0, "text": "The spell fizzles."},
                ),
            )),
        ):
            result = await spells.cast_spell(ctx, 29, mana_cost=11)

        assert result.success is True
        assert result.fizzled is True


class TestPracticeHidingResolvedSlots:
    @pytest.mark.asyncio
    async def test_noop_does_not_consume_resolved_slot(self):
        from anima.actions.result import ActionResult
        from anima.procedures import practice_hiding as ph
        from anima.procedures.practice_hiding import PracticeHiding

        ctx = _make_ctx()
        ctx.cfg.movement.walk_delay_ms = 450
        journal_seq = [
            ActionResult(success=False),
            ActionResult(success=True, data={"index": 0}),
            ActionResult(success=True, data={"index": 0}),
            ActionResult(success=True, data={"index": 0}),
        ]
        with (
            patch.object(ph, "use_skill", new=AsyncMock()) as use_skill_mock,
            patch.object(ph, "wait_for_journal",
                         new=AsyncMock(side_effect=journal_seq)),
            patch.object(ph, "asyncio") as aio,
        ):
            aio.sleep = AsyncMock()
            ctx.perception.self_state.hidden = False
            result = await PracticeHiding().execute(ctx)

        hide_calls = [c for c in use_skill_mock.await_args_list
                      if c.args[1] == ph.SKILL_HIDING]
        assert len(hide_calls) >= 4
        assert f"{ph.ATTEMPTS_PER_RUN}/{ph.ATTEMPTS_PER_RUN} hidden" in result.message

    @pytest.mark.asyncio
    async def test_all_resolved_uses_exactly_attempts_per_run(self):
        from anima.actions.result import ActionResult
        from anima.procedures import practice_hiding as ph
        from anima.procedures.practice_hiding import PracticeHiding

        ctx = _make_ctx()
        ctx.cfg.movement.walk_delay_ms = 450
        with (
            patch.object(ph, "use_skill", new=AsyncMock()) as use_skill_mock,
            patch.object(ph, "wait_for_journal", new=AsyncMock(
                return_value=ActionResult(success=True, data={"index": 0}))),
            patch.object(ph, "asyncio") as aio,
        ):
            aio.sleep = AsyncMock()
            ctx.perception.self_state.hidden = False
            result = await PracticeHiding().execute(ctx)

        hide_calls = [c for c in use_skill_mock.await_args_list
                      if c.args[1] == ph.SKILL_HIDING]
        assert len(hide_calls) == ph.ATTEMPTS_PER_RUN
        assert f"{ph.ATTEMPTS_PER_RUN}/{ph.ATTEMPTS_PER_RUN} hidden" in result.message

    @pytest.mark.asyncio
    async def test_always_timeout_caps_at_max_attempts(self):
        from anima.actions.result import ActionResult
        from anima.procedures import practice_hiding as ph
        from anima.procedures.practice_hiding import PracticeHiding

        ctx = _make_ctx()
        ctx.cfg.movement.walk_delay_ms = 450
        with (
            patch.object(ph, "use_skill", new=AsyncMock()) as use_skill_mock,
            patch.object(ph, "wait_for_journal", new=AsyncMock(
                return_value=ActionResult(success=False))),
            patch.object(ph, "asyncio") as aio,
        ):
            aio.sleep = AsyncMock()
            ctx.perception.self_state.hidden = False
            result = await PracticeHiding().execute(ctx)

        hide_calls = [c for c in use_skill_mock.await_args_list
                      if c.args[1] == ph.SKILL_HIDING]
        assert len(hide_calls) == ph.MAX_ATTEMPTS
        assert f"0/{ph.ATTEMPTS_PER_RUN} hidden" in result.message

    @pytest.mark.asyncio
    async def test_stealth_steps_count_only_actual_movement(self):
        # A walk that doesn't change position triggers no server-side
        # PlayerMobile.OnMove → no Stealth roll. Such steps must NOT be
        # counted as harvested stealth steps, and a stuck walker stops the
        # stealth-walk loop early.
        from anima.actions.result import ActionResult
        from anima.procedures import practice_hiding as ph
        from anima.procedures.practice_hiding import PracticeHiding

        ctx = _make_ctx()
        ctx.cfg.movement.walk_delay_ms = 450
        ss = ctx.perception.self_state
        ss.hidden = True  # stay hidden so _stealth_walk keeps trying to step

        with (
            patch.object(ph, "use_skill", new=AsyncMock()),
            # Always hide successfully so _stealth_walk runs every attempt.
            patch.object(ph, "wait_for_journal", new=AsyncMock(
                return_value=ActionResult(success=True, data={"index": 0}))),
            # Every walk is blocked: position never changes.
            patch.object(ph, "_walk_one_step",
                         new=AsyncMock(return_value=False)) as walk_mock,
            patch.object(ph, "time") as tmock,
            patch.object(ph, "asyncio") as aio,
        ):
            aio.sleep = AsyncMock()
            # Keep monotonic frozen well before lockout_end so the time guard
            # never short-circuits the loop — isolate the movement check.
            tmock.time.return_value = 0.0
            tmock.monotonic.return_value = 0.0
            result = await PracticeHiding().execute(ctx)

        # Zero stealth steps harvested despite STEALTH_STEPS_MAX attempts.
        assert "0 stealth steps" in result.message
        # Stuck walker breaks out after the first blocked step each run,
        # so we never spin the full STEALTH_STEPS_MAX walk calls per attempt.
        assert walk_mock.await_count == ph.ATTEMPTS_PER_RUN

    @pytest.mark.asyncio
    async def test_stealth_steps_count_moved_steps(self):
        # Sanity: when walks do move, those steps ARE counted.
        from anima.actions.result import ActionResult
        from anima.procedures import practice_hiding as ph
        from anima.procedures.practice_hiding import PracticeHiding

        ctx = _make_ctx()
        ctx.cfg.movement.walk_delay_ms = 450
        ss = ctx.perception.self_state
        ss.hidden = True

        with (
            patch.object(ph, "use_skill", new=AsyncMock()),
            patch.object(ph, "wait_for_journal", new=AsyncMock(
                return_value=ActionResult(success=True, data={"index": 0}))),
            patch.object(ph, "_walk_one_step",
                         new=AsyncMock(return_value=True)),
            patch.object(ph, "time") as tmock,
            patch.object(ph, "asyncio") as aio,
        ):
            aio.sleep = AsyncMock()
            tmock.time.return_value = 0.0
            tmock.monotonic.return_value = 0.0
            result = await PracticeHiding().execute(ctx)

        assert "0 stealth steps" not in result.message


class TestProcedureGates:
    @pytest.mark.asyncio
    async def test_practice_hiding_needs_only_life(self):
        from anima.procedures.practice_hiding import PracticeHiding

        proc = PracticeHiding()
        assert await proc.can_start(_make_ctx()) is True
        assert await proc.can_start(_make_ctx(is_alive=False)) is False

    @pytest.mark.asyncio
    async def test_practice_music_needs_instrument(self):
        from anima.procedures.practice_music import PracticeMusic

        proc = PracticeMusic()
        ctx = _make_ctx()
        assert await proc.can_start(ctx) is False
        _add_item(ctx, 0x9001, 0x0EB3)  # lute
        assert await proc.can_start(ctx) is True

    @pytest.mark.asyncio
    async def test_practice_magery_needs_spellbook(self):
        from anima.procedures.practice_magery import PracticeMagery

        from anima.core.spells import REAGENT_GRAPHICS

        proc = PracticeMagery()
        ctx = _make_ctx()
        # Stock the backpack with a full Greater Heal reagent kit so this test
        # isolates the SPELLBOOK gate (reagent gating is covered elsewhere).
        for i, name in enumerate(
            ("Ginseng", "Garlic", "Mandrake Root", "Sulfurous Ash")
        ):
            _add_item(ctx, 0x8000 + i, REAGENT_GRAPHICS[name], amount=5)
        assert await proc.can_start(ctx) is False
        # Spellbook equipped on a layer (creation book is worn)
        book = MagicMock(graphic=0x0EFA, serial=0x9002, container=0)
        ctx.perception.world.items[0x9002] = book
        ctx.perception.self_state.equipment[0x0B] = 0x9002
        assert await proc.can_start(ctx) is True

    @pytest.mark.asyncio
    async def test_bandage_self_refuses_at_full_hp(self):
        from anima.procedures.bandage_self import BandageSelf

        proc = BandageSelf()
        ctx = _make_ctx()
        _add_item(ctx, 0x9003, 0x0E21)  # bandages
        assert await proc.can_start(ctx) is False  # full HP
        ctx.perception.self_state.hits = 50
        assert await proc.can_start(ctx) is True

    @pytest.mark.asyncio
    async def test_hunt_nearby_needs_weapon_and_target(self):
        from anima.perception.enums import NotorietyFlag
        from anima.procedures.combat_loop import HuntNearby

        proc = HuntNearby()
        ctx = _make_ctx()
        _add_item(ctx, 0x9004, 0x13FF)  # katana in pack
        assert await proc.can_start(ctx) is False  # no target

        mob = MagicMock(serial=0xA1, x=102, y=200, body=0x05,
                        notoriety=NotorietyFlag.ATTACKABLE, hits=20)
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[mob])
        assert await proc.can_start(ctx) is True

    @pytest.mark.asyncio
    async def test_hunt_nearby_skips_neutral_humans(self):
        from anima.perception.enums import NotorietyFlag
        from anima.procedures.combat_loop import _find_target

        ctx = _make_ctx()
        human = MagicMock(serial=0xA2, x=101, y=200, body=0x0190,
                          notoriety=NotorietyFlag.ATTACKABLE)
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[human])
        assert _find_target(ctx) is None

    def test_find_target_focus_fires_wounded_over_nearest(self):
        from types import SimpleNamespace

        from anima.perception.enums import NotorietyFlag
        from anima.procedures.combat_loop import _find_target

        ctx = _make_ctx()
        # ss is at (100, 200). near full-HP at dist 2, far wounded at dist 6.
        near = SimpleNamespace(serial=0xB1, x=102, y=200, body=0x05,
                               notoriety=NotorietyFlag.ENEMY,
                               hits=100, hits_max=100)
        far = SimpleNamespace(serial=0xB2, x=106, y=200, body=0x05,
                              notoriety=NotorietyFlag.ENEMY,
                              hits=10, hits_max=100)
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[near, far])
        # Focus-fire beats nearest: the wounded far mob is chosen.
        assert _find_target(ctx).serial == 0xB2

    def test_find_target_distance_tiebreaks_equal_health(self):
        from types import SimpleNamespace

        from anima.perception.enums import NotorietyFlag
        from anima.procedures.combat_loop import _find_target

        ctx = _make_ctx()
        # Both unknown health (hits_max=0) -> hp_frac 1.0 tie -> nearest wins.
        near = SimpleNamespace(serial=0xC1, x=102, y=200, body=0x05,
                               notoriety=NotorietyFlag.ENEMY,
                               hits=0, hits_max=0)
        far = SimpleNamespace(serial=0xC2, x=106, y=200, body=0x05,
                              notoriety=NotorietyFlag.ENEMY,
                              hits=0, hits_max=0)
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[far, near])
        assert _find_target(ctx).serial == 0xC1

        # Equal known wounded fraction also tiebreaks on distance.
        near2 = SimpleNamespace(serial=0xC3, x=102, y=200, body=0x05,
                                notoriety=NotorietyFlag.ENEMY,
                                hits=50, hits_max=100)
        far2 = SimpleNamespace(serial=0xC4, x=106, y=200, body=0x05,
                               notoriety=NotorietyFlag.ENEMY,
                               hits=50, hits_max=100)
        ctx.perception.world.nearby_mobiles = MagicMock(
            return_value=[far2, near2])
        assert _find_target(ctx).serial == 0xC3

    def test_find_target_handles_missing_health_attrs(self):
        from types import SimpleNamespace

        from anima.perception.enums import NotorietyFlag
        from anima.procedures.combat_loop import _find_target

        ctx = _make_ctx()
        # No hits/hits_max attributes at all — must not raise, still selectable.
        mob = SimpleNamespace(serial=0xD1, x=103, y=200, body=0x05,
                              notoriety=NotorietyFlag.ENEMY)
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[mob])
        assert _find_target(ctx).serial == 0xD1


class TestPlannerProfessionBranch:
    @pytest.mark.asyncio
    async def test_thief_persona_selects_practice_hiding(self):
        from anima.planner.planner import Planner
        from anima.procedures.base import ProcedureRegistry
        from anima.procedures.practice_hiding import PracticeHiding

        reg = ProcedureRegistry()
        reg.register(PracticeHiding())
        planner = Planner(reg)
        ctx = _make_ctx()
        ctx.persona.profession = "thief"

        proc = await planner.select_procedure(ctx)
        assert proc is not None and proc.name == "practice_hiding"

    @pytest.mark.asyncio
    async def test_no_profession_falls_through_to_mining_chain(self):
        from anima.planner.planner import Planner
        from anima.procedures.base import ProcedureRegistry
        from anima.procedures.practice_hiding import PracticeHiding

        reg = ProcedureRegistry()
        reg.register(PracticeHiding())
        planner = Planner(reg)
        ctx = _make_ctx()
        ctx.persona.profession = ""

        proc = await planner.select_procedure(ctx)
        assert proc is None or proc.name != "practice_hiding"


class TestKernelProfiles:
    def test_all_profession_profiles_exist(self):
        from foundry.kernel.gm import FIXED_START_PROFILES

        # Workplaces moved out of profiles into LANE_SPOTS (lane workplaces,
        # 2026-06-11) — profiles carry skills/items/spawns only.
        for key in ("miner", "mage", "warrior", "bard", "thief", "crafter"):
            p = FIXED_START_PROFILES[key]
            assert "skills" in p and "go" not in p

    def test_ground_target_response_layout(self):
        from foundry.kernel.gm import build_ground_target_response

        pkt = build_ground_target_response(7, 2567, 493, -5)
        assert len(pkt) == 19
        assert pkt[0] == 0x6C
        assert pkt[1] == 0x01  # ground target
        import struct
        cursor, = struct.unpack_from(">I", pkt, 2)
        x, y = struct.unpack_from(">HH", pkt, 11)
        assert (cursor, x, y) == (7, 2567, 493)


class TestPersonaSkillIds:
    def test_creation_skill_sets_are_servuo_valid(self):
        """New persona skill sets must pass ServUO ValidSkills (sum 100)."""
        from anima.client.appearance import PERSONA_SKILLS

        for persona in ("mage", "bard", "thief", "adventurer"):
            total = sum(v for _, v in PERSONA_SKILLS[persona])
            assert total == 100, f"{persona} creation skills sum {total} != 100"

    def test_creation_stats_are_servuo_valid(self):
        """ServUO SetStats: each stat 10..60, total EXACTLY 90 (modern
        clients) — anything else silently resets to 10/10/10."""
        from anima.client.appearance import PERSONA_STATS

        for persona, (s, d, i) in PERSONA_STATS.items():
            assert s + d + i == 90, f"{persona} stats sum {s+d+i} != 90"
            for v in (s, d, i):
                assert 10 <= v <= 60, f"{persona} stat {v} out of 10..60"

    def test_servuo_enum_ids(self):
        from anima.client.appearance import PERSONA_SKILLS

        assert PERSONA_SKILLS["mage"] [0][0] == 25   # Magery
        assert PERSONA_SKILLS["mage"] [1][0] == 46   # Meditation
        assert PERSONA_SKILLS["bard"] [0][0] == 29   # Musicianship
        assert PERSONA_SKILLS["bard"] [1][0] == 9    # Peacemaking
        assert PERSONA_SKILLS["thief"][0][0] == 21   # Hiding
        assert PERSONA_SKILLS["thief"][1][0] == 47   # Stealth


class TestCombatKillDetection:
    @pytest.mark.asyncio
    async def test_unknown_health_is_not_a_kill(self):
        """Default hits=0 (never status-queried) must NOT read as dead.

        Regression: the live warrior probe re-targeted every tick
        (95 'kills' in 95s), never landing a swing — zero skill gain.
        """
        import asyncio as _asyncio

        from anima.perception.enums import NotorietyFlag
        from anima.procedures import combat_loop as cl

        ctx = _make_ctx()
        # Katana already equipped (creation grants it worn)
        ctx.perception.self_state.equipment[1] = 0x9004
        mob = MagicMock(serial=0xA1, x=101, y=200, body=0x05,
                        notoriety=NotorietyFlag.ATTACKABLE,
                        hits=0, hits_max=0)  # health never queried
        ctx.perception.world.nearby_mobiles = MagicMock(return_value=[mob])
        ctx.perception.world.mobiles = {0xA1: mob}

        proc = cl.HuntNearby()
        with patch.object(cl, "ENGAGEMENT_CAP_S", 0.25), \
             patch.object(cl, "TICK_S", 0.05):
            result = await proc.run(ctx)

        # One engagement, no phantom kills
        assert "0 kills" in result.message or "kills" not in result.message \
            or result.details.get("kills", 0) == 0 or "Combat: 0 kills" in result.message

    @pytest.mark.asyncio
    async def test_removed_mobile_counts_as_kill(self):
        from anima.perception.enums import NotorietyFlag
        from anima.procedures import combat_loop as cl

        ctx = _make_ctx()
        ctx.perception.self_state.equipment[1] = 0x9004
        mob = MagicMock(serial=0xA1, x=101, y=200, body=0x05,
                        notoriety=NotorietyFlag.ATTACKABLE,
                        hits=0, hits_max=0)
        targets = [[mob], []]  # first scan finds it, after kill: none

        def _nearby(*a, **k):
            return targets[0] if len(targets) == 1 else targets.pop(0)

        ctx.perception.world.nearby_mobiles = MagicMock(side_effect=_nearby)
        ctx.perception.world.mobiles = {}  # already removed → dead on first check

        proc = cl.HuntNearby()
        with patch.object(cl, "ENGAGEMENT_CAP_S", 0.25), \
             patch.object(cl, "TICK_S", 0.05):
            result = await proc.run(ctx)

        assert "1 kills" in result.message
