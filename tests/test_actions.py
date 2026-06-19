"""Tests for action primitives (v2 migration Step 2)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.actions.result import ActionResult
from anima.core.bus import EventBus


class TestActionResult:
    def test_success(self):
        r = ActionResult(success=True, message="ok")
        assert r.success
        assert r.message == "ok"

    def test_failure(self):
        r = ActionResult(success=False, message="timeout")
        assert not r.success

    def test_data(self):
        r = ActionResult(success=True, data={"cursor_id": 42})
        assert r.data["cursor_id"] == 42


class TestWaitForCondition:
    @pytest.mark.asyncio
    async def test_already_true(self):
        bus = EventBus()
        result = await bus.wait_for_condition(lambda: True, timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_becomes_true(self):
        bus = EventBus()
        flag = {"ready": False}

        async def set_flag():
            await asyncio.sleep(0.05)
            flag["ready"] = True
            bus.publish("test.event", {})

        task = asyncio.create_task(set_flag())
        result = await bus.wait_for_condition(lambda: flag["ready"], timeout=2.0)
        assert result is True
        await task

    @pytest.mark.asyncio
    async def test_timeout(self):
        bus = EventBus()
        result = await bus.wait_for_condition(lambda: False, timeout=0.1)
        assert result is False

    @pytest.mark.asyncio
    async def test_race_after_subscribe(self):
        """Predicate becomes true between subscribe and wait."""
        bus = EventBus()
        # The predicate is already true — should return immediately
        result = await bus.wait_for_condition(lambda: True, timeout=0.1)
        assert result is True


class TestTargetPrimitives:
    @pytest.mark.asyncio
    async def test_wait_for_target_success(self):
        from anima.actions.target import wait_for_target

        ctx = MagicMock()
        ctx.perception.self_state.pending_target = None
        bus = EventBus()
        ctx.bus = bus

        async def simulate_target():
            await asyncio.sleep(0.05)
            # Use the exact keys the real 0x6C handler stores.
            ctx.perception.self_state.pending_target = {
                "cursor_id": 123,
                "target_type": 0,
                "cursor_flag": 1,
            }
            bus.publish("avatar.target_cursor", {})

        task = asyncio.create_task(simulate_target())
        result = await wait_for_target(ctx, timeout=2.0)
        assert result.success
        assert result.data["cursor_id"] == 123
        # The server's harmful/helpful flag must survive to the caller so it
        # can be echoed back; previously it was always dropped (read from a
        # nonexistent "cursor_type" key).
        assert result.data["cursor_flag"] == 1
        assert result.data["target_type"] == 0
        await task

    @pytest.mark.asyncio
    async def test_wait_for_target_reads_handler_keys(self):
        """wait_for_target must read the keys the live 0x6C handler sets.

        Regression for the silent flag-drop: the handler stores
        ``cursor_flag``/``target_type``; the consumer used to read a
        ``cursor_type`` key that was never written, so a helpful (2) cursor
        looked neutral (0) and the echoed response could be rejected.
        """
        from anima.actions.target import wait_for_target

        ctx = MagicMock()
        bus = EventBus()
        ctx.bus = bus
        # Mirror perception/handlers.py handle_target_cursor for a heal cursor.
        ctx.perception.self_state.pending_target = {
            "target_type": 0,
            "cursor_id": 0xDEADBEEF,
            "cursor_flag": 2,  # helpful
        }

        result = await wait_for_target(ctx, timeout=1.0)
        assert result.success
        assert result.data["cursor_flag"] == 2
        # pending_target consumed exactly once
        assert ctx.perception.self_state.pending_target is None

    @pytest.mark.asyncio
    async def test_cast_spell_echoes_cursor_flag(self):
        """cast_spell echoes the server cursor flag on the target response."""
        from anima.actions import spells
        from anima.actions.result import ActionResult

        ctx = MagicMock()
        ctx.conn.send_packet = AsyncMock()
        ss = ctx.perception.self_state
        ss.mana = 50
        ss.mana_max = 50
        ss.serial = 0x0001A2B3
        ss.pending_target = None

        captured: dict = {}

        async def fake_wait_for_target(_ctx, timeout=3.0):
            # Server requested a harmful object cursor.
            return ActionResult(
                success=True,
                data={"cursor_id": 0x55, "target_type": 0, "cursor_flag": 1},
            )

        async def fake_target_object(_ctx, cursor_id, serial, cursor_flag=0):
            captured["cursor_id"] = cursor_id
            captured["serial"] = serial
            captured["cursor_flag"] = cursor_flag
            return ActionResult(success=True)

        async def fake_wait_for_journal(_ctx, _signals, timeout=1.5, since=0.0):
            # No abort/fizzle text — clean resolution.
            return ActionResult(success=False)

        with patch.object(spells, "wait_for_target", fake_wait_for_target), \
             patch.object(spells, "target_object", fake_target_object), \
             patch.object(spells, "wait_for_journal", fake_wait_for_journal):
            res = await spells.cast_spell(
                ctx, spells.SPELL_HEAL, target_serial=0x77, mana_cost=0,
            )

        assert res.success
        assert captured["cursor_flag"] == 1
        assert captured["serial"] == 0x77

    @pytest.mark.asyncio
    async def test_wait_for_target_timeout(self):
        from anima.actions.target import wait_for_target

        ctx = MagicMock()
        ctx.perception.self_state.pending_target = None
        ctx.bus = EventBus()

        result = await wait_for_target(ctx, timeout=0.1)
        assert not result.success
        assert "timeout" in result.message.lower()

    @pytest.mark.asyncio
    async def test_target_tile(self):
        from anima.actions.target import target_tile

        ctx = MagicMock()
        ctx.conn.send_packet = AsyncMock()

        result = await target_tile(ctx, cursor_id=1, x=100, y=200, z=15, graphic=0x021A)
        assert result.success
        ctx.conn.send_packet.assert_called_once()

    @pytest.mark.asyncio
    async def test_target_object(self):
        from anima.actions.target import target_object

        ctx = MagicMock()
        ctx.conn.send_packet = AsyncMock()

        result = await target_object(ctx, cursor_id=1, serial=0x40001234)
        assert result.success
        assert result.data["serial"] == 0x40001234


class TestInventoryPrimitives:
    def test_find_in_backpack_found(self):
        from anima.actions.inventory import find_in_backpack

        ctx = MagicMock()
        ctx.perception.self_state.equipment = {0x15: 0x101}  # backpack

        item1 = MagicMock(container=0x101, graphic=0x19B9, amount=5)
        item2 = MagicMock(container=0x101, graphic=0x1BF2, amount=3)
        item3 = MagicMock(container=0x999, graphic=0x19B9, amount=10)  # different container
        ctx.perception.world.items = {1: item1, 2: item2, 3: item3}

        found = find_in_backpack(ctx, {0x19B9})
        assert len(found) == 1
        assert found[0].amount == 5

    def test_find_in_backpack_empty(self):
        from anima.actions.inventory import find_in_backpack

        ctx = MagicMock()
        ctx.perception.self_state.equipment = {0x15: 0x101}
        ctx.perception.world.items = {}

        found = find_in_backpack(ctx, {0x19B9})
        assert found == []

    def test_count_items(self):
        from anima.actions.inventory import count_items

        ctx = MagicMock()
        ctx.perception.self_state.equipment = {0x15: 0x101}

        item1 = MagicMock(container=0x101, graphic=0x19B9, amount=5)
        item2 = MagicMock(container=0x101, graphic=0x19B9, amount=12)
        ctx.perception.world.items = {1: item1, 2: item2}

        assert count_items(ctx, {0x19B9}) == 17

    @pytest.mark.asyncio
    async def test_drag_drop(self):
        from anima.actions.inventory import drag_drop

        ctx = MagicMock()
        ctx.conn.send_packet = AsyncMock()

        result = await drag_drop(ctx, item_serial=0x40001, amount=10, target_serial=0x40002)
        assert result.success
        assert ctx.conn.send_packet.call_count == 2  # pick_up + drop_item


class TestGoToInterrupt:
    @pytest.mark.asyncio
    async def test_interrupt_callback(self):
        """go_to should return False when interrupt_check returns True."""
        from anima.action.movement import go_to

        ctx = MagicMock()
        ctx.perception.self_state.x = 100
        ctx.perception.self_state.y = 100
        ctx.perception.self_state.z = 0
        ctx.conn.connected = True
        ctx.cfg.movement.walk_delay_ms = 200

        # Interrupt immediately
        result = await go_to(ctx, 200, 200, interrupt_check=lambda: True)
        assert result is False

    @pytest.mark.asyncio
    async def test_no_interrupt_none(self):
        """go_to should work normally when interrupt_check is None (default)."""
        from anima.action.movement import go_to

        ctx = MagicMock()
        ctx.perception.self_state.x = 100
        ctx.perception.self_state.y = 101  # Already within 1 tile of target
        ctx.perception.self_state.z = 0
        ctx.conn.connected = True
        ctx.cfg.movement.walk_delay_ms = 200
        ctx.walker.denied_tiles = {}

        # Already at target (within 1 tile) → should succeed
        result = await go_to(ctx, 100, 101, interrupt_check=None)
        assert result is True


class TestFindClosedDoorAt:
    """Regression tests: door logic must only fire when path actually crosses
    the door tile, not when a door is merely adjacent to the path tile.

    Real-world bug (2026-04-18): agent at (2524,547) tried to walk east to
    (2525,547). A door existed at (2525,548) — Chebyshev-1 adjacent to the
    path tile but NOT on the path. The old loose adjacency match triggered
    door traversal from a diagonal position; the server silently ignored
    the double-click and the agent looped forever.
    """

    def _make_ctx(self, door_x: int, door_y: int):
        from anima.perception.world_state import ItemInfo, WorldState

        ctx = MagicMock()
        ctx.perception.world = WorldState()
        door = ItemInfo(serial=0x40000CCE, x=door_x, y=door_y, graphic=0x06AE, container=0)
        ctx.perception.world.items[door.serial] = door

        from anima.map import FLAG_DOOR, FLAG_IMPASSABLE
        ctx.map_reader._get_item_flags.return_value = FLAG_DOOR | FLAG_IMPASSABLE
        return ctx

    def test_returns_serial_when_path_tile_is_door_tile(self):
        from anima.action.movement import _find_closed_door_at

        ctx = self._make_ctx(door_x=2525, door_y=548)
        # Path tile == door position → must return door serial
        assert _find_closed_door_at(ctx, 2525, 548) == 0x40000CCE

    def test_returns_none_when_door_only_adjacent_to_path_tile(self):
        from anima.action.movement import _find_closed_door_at

        ctx = self._make_ctx(door_x=2525, door_y=548)
        # Path tile (2525,547) is NORTH of door (2525,548) — door is adjacent,
        # but path does not cross it. Door logic must NOT fire here, otherwise
        # the agent will try to open a door from a non-cardinal position and
        # loop endlessly when the server ignores it.
        assert _find_closed_door_at(ctx, 2525, 547) is None

    def test_returns_none_when_door_diagonal_to_path_tile(self):
        from anima.action.movement import _find_closed_door_at

        ctx = self._make_ctx(door_x=2525, door_y=548)
        # Diagonal neighbor (2524, 547) — also must not match.
        assert _find_closed_door_at(ctx, 2524, 547) is None


# ---------------------------------------------------------------------------
# Run (달리기) activation in go_to
# ---------------------------------------------------------------------------


class TestRunActivation:
    def _ss(self, stam=100, stam_max=100):
        ss = MagicMock()
        ss.stam = stam
        ss.stam_max = stam_max
        return ss

    def _cfg(self, run_enabled=True):
        cfg = MagicMock()
        cfg.movement.run_enabled = run_enabled
        return cfg

    def test_should_run_long_leg_full_stam(self):
        from anima.action.movement import _should_run

        assert _should_run(self._ss(), remaining=20, cfg=self._cfg(), override=None)

    def test_should_run_near_target_walks(self):
        from anima.action.movement import _should_run

        assert not _should_run(self._ss(), remaining=3, cfg=self._cfg(), override=None)

    def test_should_run_low_stamina_walks(self):
        from anima.action.movement import _should_run

        assert not _should_run(
            self._ss(stam=10, stam_max=100), remaining=20,
            cfg=self._cfg(), override=None,
        )

    def test_should_run_override_wins(self):
        from anima.action.movement import _should_run

        assert _should_run(
            self._ss(stam=1, stam_max=100), remaining=1,
            cfg=self._cfg(), override=True,
        )
        assert not _should_run(
            self._ss(), remaining=50, cfg=self._cfg(), override=False,
        )

    def test_should_run_disabled_by_config(self):
        from anima.action.movement import _should_run

        assert not _should_run(
            self._ss(), remaining=50, cfg=self._cfg(run_enabled=False),
            override=None,
        )

    def test_should_run_unknown_stamina_walks(self):
        from anima.action.movement import _should_run

        assert not _should_run(
            self._ss(stam=0, stam_max=0), remaining=50,
            cfg=self._cfg(), override=None,
        )

    def test_run_bit_in_walk_packet(self):
        from anima.client.packets import build_walk_request

        pkt = build_walk_request(2 | 0x80, 1, 0)
        assert pkt[0] == 0x02
        assert pkt[1] == 0x82

    @pytest.mark.asyncio
    async def test_go_to_runs_far_walks_near(self):
        """go_to sets the 0x80 run bit on long legs, drops it near target."""
        from anima.action import movement
        from anima.action.movement import go_to

        ctx = MagicMock()
        ss = ctx.perception.self_state
        ss.x, ss.y, ss.z = 100, 100, 0
        ss.direction = 2  # already facing east — no turn packets
        ss.stam, ss.stam_max = 100, 100
        ctx.conn.connected = True
        ctx.cfg.movement.walk_delay_ms = 1
        ctx.cfg.movement.run_delay_ms = 1
        ctx.cfg.movement.run_enabled = True
        ctx.walker.denied_tiles = {}
        ctx.walker.next_sequence = MagicMock(return_value=1)
        ctx.walker.pop_fast_walk_key = MagicMock(return_value=0)

        sent_dirs: list[int] = []
        remaining_at_send: list[int] = []
        target_x = 130

        async def _send(pkt: bytes):
            if pkt and pkt[0] == 0x02:
                sent_dirs.append(pkt[1])
                remaining_at_send.append(target_x - ss.x)
                ss.x += 1  # server confirms the eastward step

        ctx.conn.send_packet = AsyncMock(side_effect=_send)

        def _fake_path(mr, sx, sy, tx, ty, **kw):
            return [(x, 100) for x in range(sx + 1, target_x)]

        with (
            patch.object(movement, "find_path", side_effect=_fake_path),
            patch.object(movement, "_get_door_positions", return_value=set()),
            patch.object(movement, "_find_closed_door_at", return_value=None),
        ):
            arrived = await go_to(ctx, target_x, 100)

        assert arrived is True
        assert sent_dirs, "no walk packets captured"
        for dir_byte, rem in zip(sent_dirs, remaining_at_send):
            if rem >= movement.RUN_MIN_REMAINING:
                assert dir_byte & 0x80, f"expected run bit at remaining={rem}"
            else:
                assert not (dir_byte & 0x80), f"expected walk at remaining={rem}"


# ---------------------------------------------------------------------------
# anima.actions façade + docs/actions.md sync
# ---------------------------------------------------------------------------


class TestActionsFacade:
    def test_all_exports_resolve(self):
        """Every façade export must import and be callable (or a type)."""
        import anima.actions as actions

        for name in actions.__all__:
            obj = getattr(actions, name)
            assert callable(obj) or isinstance(obj, type), name

    def test_unknown_attribute_raises(self):
        import anima.actions as actions

        with pytest.raises(AttributeError):
            actions.definitely_not_a_primitive  # noqa: B018

    def test_docs_catalog_covers_facade_and_procedures(self):
        """docs/actions.md must mention every export and procedure name."""
        from pathlib import Path

        import anima.actions as actions

        doc = (Path(__file__).parent.parent / "docs" / "actions.md").read_text()
        for name in actions.__all__:
            assert name in doc, f"docs/actions.md missing façade export: {name}"

        import re
        proc_dir = Path(__file__).parent.parent / "anima" / "procedures"
        for py in proc_dir.glob("*.py"):
            for proc_name in re.findall(
                r'^\s{4}name = "([a-z_]+)"$', py.read_text(), re.MULTILINE,
            ):
                assert proc_name in doc, (
                    f"docs/actions.md missing procedure: {proc_name} ({py.name})"
                )


# ---------------------------------------------------------------------------
# wait_for_journal — no-bus polling fallback
# ---------------------------------------------------------------------------


class TestWaitForJournalNoBus:
    """When ctx.bus is None the helper must still poll the journal instead of
    failing instantly. Regression: the original code only had a bus branch, so
    an already-present matching entry was reported as a timeout."""

    def _make_ctx(self):
        import time

        from anima.perception.enums import MessageType
        from anima.perception.social_state import SocialState

        ctx = MagicMock()
        ctx.bus = None
        social = SocialState()
        ctx.perception.social = social
        return ctx, social, MessageType, time

    @pytest.mark.asyncio
    async def test_matches_existing_entry_without_bus(self):
        from anima.actions.journal import wait_for_journal

        ctx, social, MessageType, _time = self._make_ctx()
        # Entry already present (timestamp in the future relative to `since`
        # is guaranteed because add_speech stamps time.time() at call).
        social.add_speech(
            serial=1, name="Forge", text="You put the ore into the forge.",
            msg_type=MessageType.SYSTEM,
        )

        result = await wait_for_journal(
            ctx, ["into the forge"], timeout=1.0, since=0.0,
        )
        assert result.success
        assert result.data["index"] == 0
        assert "forge" in result.data["text"].lower()

    @pytest.mark.asyncio
    async def test_times_out_without_bus_when_no_match(self):
        from anima.actions.journal import wait_for_journal

        ctx, social, MessageType, _time = self._make_ctx()
        social.add_speech(
            serial=1, name="Forge", text="something unrelated",
            msg_type=MessageType.SYSTEM,
        )

        result = await wait_for_journal(
            ctx, ["into the forge"], timeout=0.2, since=0.0,
        )
        assert not result.success
        assert "timeout" in result.message.lower()

    @pytest.mark.asyncio
    async def test_matches_entry_arriving_after_call_without_bus(self):
        from anima.actions.journal import wait_for_journal

        ctx, social, MessageType, _time = self._make_ctx()

        async def _emit():
            await asyncio.sleep(0.05)
            social.add_speech(
                serial=1, name="Forge", text="You smelt the ore.",
                msg_type=MessageType.SYSTEM,
            )

        task = asyncio.create_task(_emit())
        result = await wait_for_journal(
            ctx, ["smelt the ore"], timeout=2.0, since=0.0,
        )
        assert result.success
        await task
