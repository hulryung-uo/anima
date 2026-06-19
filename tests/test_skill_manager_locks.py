"""Tests for apply_skill_locks (anima/skills/skill_manager.py).

The key regression guarded here: a persona's ``skills_up`` targets must each
receive an explicit Up (0x3A) lock packet even when the server has not yet
reported that skill in ``self_state.skills``. The previous implementation only
iterated over already-reported skills, so a fresh character's core skills
(value 0.0, absent from the dict) silently never got their Up lock and would
never train.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from anima.perception.enums import Lock
from anima.perception.self_state import SkillInfo
from anima.skills.skill_manager import (
    PERSONA_SKILL_PROFILES,
    SKILL_HEALING,
    SKILL_MINING,
    SKILL_SWORDSMANSHIP,
    SKILL_TACTICS,
    apply_skill_locks,
)


def _make_ctx(skills: dict[int, SkillInfo]) -> SimpleNamespace:
    self_state = SimpleNamespace(skills=skills)
    perception = SimpleNamespace(self_state=self_state)
    conn = SimpleNamespace(send_packet=AsyncMock())
    return SimpleNamespace(perception=perception, conn=conn)


def _decode_skill_lock(data: bytes) -> tuple[int, int]:
    """Return (skill_id, lock_state) from a 0x3A SkillLock packet."""
    assert data[0] == 0x3A
    length = (data[1] << 8) | data[2]
    assert length == len(data)
    skill_id = (data[3] << 8) | data[4]
    lock_state = data[5]
    return skill_id, lock_state


def _skill_lock_calls(conn) -> dict[int, int]:
    """Map skill_id -> last lock_state for every 0x3A packet sent."""
    out: dict[int, int] = {}
    for call in conn.send_packet.await_args_list:
        data = call.args[0]
        if data[0] == 0x3A:
            sid, lock = _decode_skill_lock(data)
            out[sid] = lock
    return out


@pytest.mark.asyncio
async def test_persona_target_absent_from_dict_still_gets_up_lock() -> None:
    # Fresh character: server has reported nothing yet.
    ctx = _make_ctx({})

    sent = await apply_skill_locks(ctx, "adventurer")

    locks = _skill_lock_calls(ctx.conn)
    profile = PERSONA_SKILL_PROFILES["adventurer"]
    # Every skills_up target must have been sent an explicit Up lock.
    for skill_id in profile["skills_up"]:
        assert locks.get(skill_id) == Lock.UP.value, (
            f"skill {skill_id} should be set Up even when absent from dict"
        )
    # And we sent at least one packet per target plus the 3 stat locks.
    assert sent >= len(profile["skills_up"]) + 3


@pytest.mark.asyncio
async def test_reported_non_target_skill_locked() -> None:
    # Server reported a skill the persona does NOT want raised; it must be
    # set to Locked.
    ctx = _make_ctx(
        {
            SKILL_MINING: SkillInfo(id=SKILL_MINING, value=50.0, lock=Lock.UP),
        }
    )

    await apply_skill_locks(ctx, "adventurer")

    locks = _skill_lock_calls(ctx.conn)
    assert locks.get(SKILL_MINING) == Lock.LOCKED.value


@pytest.mark.asyncio
async def test_already_correct_locks_are_not_resent() -> None:
    # A reported skill already at the desired lock should not trigger a packet.
    ctx = _make_ctx(
        {
            SKILL_SWORDSMANSHIP: SkillInfo(
                id=SKILL_SWORDSMANSHIP, value=70.0, lock=Lock.UP
            ),
            SKILL_MINING: SkillInfo(id=SKILL_MINING, value=0.0, lock=Lock.LOCKED),
        }
    )

    await apply_skill_locks(ctx, "adventurer")

    locks = _skill_lock_calls(ctx.conn)
    # Swordsmanship was already Up -> no packet for it.
    assert SKILL_SWORDSMANSHIP not in locks
    # Mining was already Locked -> no packet for it.
    assert SKILL_MINING not in locks
    # But other persona targets (e.g. Healing, Tactics), absent from the dict,
    # still get their Up lock.
    assert locks.get(SKILL_HEALING) == Lock.UP.value
    assert locks.get(SKILL_TACTICS) == Lock.UP.value


@pytest.mark.asyncio
async def test_unknown_persona_sends_nothing() -> None:
    ctx = _make_ctx({SKILL_MINING: SkillInfo(id=SKILL_MINING)})

    sent = await apply_skill_locks(ctx, "no_such_persona")

    assert sent == 0
    ctx.conn.send_packet.assert_not_awaited()
