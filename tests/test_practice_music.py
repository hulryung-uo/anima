"""Regression guard for the BARD Musicianship loop's phantom-success bug.

``PracticeMusic.execute`` used to return ``success=True`` unconditionally after
the ``for play in range(PLAYS_PER_RUN)`` loop — even when every ``send_packet``
landed on a dropped connection (a silent no-op that rolls NO Musicianship
CheckSkill). Musicianship is sound-only (no journal line), so a dead-connection
run is indistinguishable from a real one in the message, yet it practiced
nothing. These tests pin the contract shared by the sibling loops
(practice_peacemaking / practice_hiding / bandage_self / mine_ore): a run that
resolved ZERO rolls is a retryable failure, not a phantom success.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.practice_music as pm
from anima.procedures.practice_music import PLAYS_PER_RUN, PracticeMusic


def _ss(value=40.0):
    music = SimpleNamespace(value=value)
    skills = {pm.SKILL_MUSICIANSHIP: music}
    return SimpleNamespace(
        is_alive=True,
        serial=0x1,
        skills=SimpleNamespace(get=skills.get),
    )


def _ctx(ss, *, connected=True):
    conn = SimpleNamespace(connected=connected, send_packet=AsyncMock())
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss),
        conn=conn,
    )


@pytest.fixture(autouse=True)
def _patch_common(monkeypatch):
    # No real waiting between plays — keeps the test instant.
    monkeypatch.setattr(pm.asyncio, "sleep", AsyncMock())
    # Backpack always holds a lute.
    monkeypatch.setattr(
        pm, "find_in_backpack",
        lambda _ctx, _g: [SimpleNamespace(serial=0xAB, graphic=0x0EB3)],
    )


@pytest.mark.asyncio
async def test_disconnected_run_is_retryable_failure():
    """A run that never plays (session dropped) must be a retryable BLOCKED
    failure, never a phantom ``success=True`` for a run that practiced
    nothing — mirrors practice_peacemaking / bandage_self / mine_ore."""
    ss = _ss()
    ctx = _ctx(ss, connected=False)

    result = await PracticeMusic().execute(ctx)

    # No play packet was ever sent on the dead connection.
    assert ctx.conn.send_packet.await_count == 0
    assert result.success is False
    assert result.reason is pm.FailureReason.BLOCKED
    # No re-suggestion to chain straight back into the same stall.
    assert result.next_suggestion is None
    # Nothing rolled → nothing credited.
    assert not result.skill_gains


@pytest.mark.asyncio
async def test_live_run_succeeds_and_plays():
    """A live connection plays the full run and reports success (a failed
    Musicianship roll legitimately grants no skill, so zero-gain is still a
    real success — the gate is on plays issued, never on gain)."""
    ss = _ss(value=40.0)  # no skill delta during the run → zero gain
    ctx = _ctx(ss, connected=True)

    result = await PracticeMusic().execute(ctx)

    # At least one play packet per loop iteration was actually sent.
    assert ctx.conn.send_packet.await_count >= PLAYS_PER_RUN
    assert result.success is True
    assert result.next_suggestion == "practice_music"


@pytest.mark.asyncio
async def test_live_run_credits_gain():
    """A live run that DID gain Musicianship credits the delta."""
    music = SimpleNamespace(value=40.0)
    skills = {pm.SKILL_MUSICIANSHIP: music}
    ss = SimpleNamespace(
        is_alive=True, serial=0x1,
        skills=SimpleNamespace(get=skills.get),
    )
    ctx = _ctx(ss, connected=True)

    # Bump the skill mid-run so ``after - before`` is positive.
    orig_sleep = ctx.conn.send_packet

    async def bump(*_a, **_k):
        music.value += 0.1

    ctx.conn.send_packet = AsyncMock(side_effect=bump)

    result = await PracticeMusic().execute(ctx)

    assert result.success is True
    assert pm.SKILL_MUSICIANSHIP in result.skill_gains
    assert result.skill_gains[pm.SKILL_MUSICIANSHIP] > 0


def test_loop_guards_on_connection():
    """Source-shape guard: the play loop must bail on a dropped connection and
    fail a zero-play run, never fall through to an unconditional success."""
    import inspect

    src = inspect.getsource(PracticeMusic.execute)
    assert "ctx.conn.connected" in src
    assert "if plays == 0" in src
