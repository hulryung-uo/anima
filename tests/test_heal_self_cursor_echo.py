"""HealSelf must echo the server's real target cursor id.

ServUO's targeting layer (Server/Targeting/Target.cs) validates the cursor id
carried in the client's 0x6C TargetResponse against the id it issued for the
pending Target. A response whose cursor id does not match is dropped, so a
bandage applied with a hardcoded ``cursor_id=0`` never actually heals — the
server simply ignores it and the agent's self-heal silently no-ops.

Regression: HealSelf used to ``asyncio.sleep(0.5)`` and then reply with
``cursor_id=0`` regardless of what the server issued. This test pins the
contract that the skill (a) waits for ``self_state.pending_target`` and
(b) forwards that cursor id on the wire, exactly like mine_ore / smelt_ore.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anima.skills.combat.healing import BANDAGE_GRAPHIC, HealSelf


def _make_ctx():
    ctx = MagicMock()
    ss = ctx.perception.self_state
    ss.x, ss.y, ss.z = 100, 100, 0
    ss.serial = 0xDEADBEEF
    ss.hits = 50
    ss.hits_max = 100
    ss.hp_percent = 50.0
    ss.equipment = {0x15: 0x101}  # backpack
    ss.pending_target = None
    bandage = MagicMock(container=0x101, graphic=BANDAGE_GRAPHIC, serial=0x500, amount=5)
    ctx.perception.world.items = {0x500: bandage}
    ctx.conn.send_packet = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_heal_self_echoes_issued_cursor_id():
    skill = HealSelf()
    ctx = _make_ctx()
    ss = ctx.perception.self_state

    issued_cursor = 0x00C0FFEE
    captured: dict[str, int] = {}

    async def fake_sleep(_d):
        # The server "answers" the double-click by populating pending_target
        # with its issued cursor id during the first poll wait.
        if ss.pending_target is None:
            ss.pending_target = {"cursor_id": issued_cursor}

    def fake_target_response(*, target_type, cursor_id, serial=0, **_kw):
        captured["cursor_id"] = cursor_id
        captured["serial"] = serial
        return b"\x6c"

    with patch(
        "anima.skills.combat.healing.asyncio.sleep", new=fake_sleep,
    ), patch(
        "anima.skills.combat.healing.build_target_response",
        side_effect=fake_target_response,
    ), patch(
        "anima.skills.combat.healing.build_double_click",
        side_effect=lambda s: b"\x06",
    ):
        await skill.execute(ctx)

    # The bandage target must carry the cursor id the SERVER issued, not 0.
    assert captured.get("cursor_id") == issued_cursor, (
        f"heal_self replied with cursor_id={captured.get('cursor_id')!r}, "
        f"expected the issued {issued_cursor:#x}"
    )
    # And it must target self.
    assert captured.get("serial") == ss.serial


@pytest.mark.asyncio
async def test_heal_self_aborts_when_no_cursor_arrives():
    """If the server never opens a target cursor, the skill must fail cleanly
    rather than firing a phantom bandage target into the void."""
    skill = HealSelf()
    ctx = _make_ctx()

    sent_target = {"n": 0}

    async def fake_sleep(_d):
        # Never populate pending_target — simulate a dropped/late cursor.
        return None

    def fake_target_response(**_kw):
        sent_target["n"] += 1
        return b"\x6c"

    with patch(
        "anima.skills.combat.healing.asyncio.sleep", new=fake_sleep,
    ), patch(
        "anima.skills.combat.healing.build_target_response",
        side_effect=fake_target_response,
    ), patch(
        "anima.skills.combat.healing.build_double_click",
        side_effect=lambda s: b"\x06",
    ):
        result = await skill.execute(ctx)

    assert result.success is False
    assert sent_target["n"] == 0, "must not send a target response without a cursor"
    assert "cursor" in result.message.lower()
