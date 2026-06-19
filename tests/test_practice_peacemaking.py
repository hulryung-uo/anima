"""Regression guard for the BARD peacemaking loop's attempt accounting.

The on-disk loop once used ``for attempt in range(ATTEMPTS_PER_RUN)`` with two
``continue`` branches: a cursor miss (the agent is still inside the skill
lockout, so no target cursor lands) filled the lockout with plays and then
``continue``d — consuming one of the THREE Peacemaking attempts WITHOUT ever
rolling the skill. A run that hit two cursor misses therefore rolled Peacemaking
once instead of three times. These tests pin the g_00101-style fix: count only
RESOLVED Peacemaking checks, so a cursor miss retries instead of eating a slot.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.practice_peacemaking as pp
from anima.procedures.practice_peacemaking import (
    ATTEMPTS_PER_RUN,
    PracticePeacemaking,
)


def _ss():
    peace = SimpleNamespace(value=40.0)
    music = SimpleNamespace(value=40.0)
    skills = {pp.SKILL_PEACEMAKING: peace, pp.SKILL_MUSICIANSHIP: music}
    return SimpleNamespace(
        is_alive=True,
        serial=0x1,
        pending_target=None,
        skills=SimpleNamespace(get=skills.get),
    )


def _ok_cursor():
    return SimpleNamespace(success=True, data={"cursor_id": 0x55})


def _miss_cursor():
    return SimpleNamespace(success=False, data={})


def _ctx(ss):
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss))


@pytest.fixture(autouse=True)
def _patch_common(monkeypatch):
    # The instrument-prompt journal check (500617) never fires (we model the
    # warmed-up path where the instrument is already remembered).
    monkeypatch.setattr(
        pp, "wait_for_journal",
        AsyncMock(return_value=SimpleNamespace(success=False, data={})),
    )
    monkeypatch.setattr(pp, "use_skill", AsyncMock())
    monkeypatch.setattr(pp, "target_object", AsyncMock())
    # Avoid touching ctx.conn / sleeping: the lockout fill is a no-op here.
    monkeypatch.setattr(pp, "_play_through_lockout", AsyncMock(return_value=0))
    # backpack instrument lookup
    monkeypatch.setattr(
        pp, "find_in_backpack",
        lambda _ctx, _g: [SimpleNamespace(serial=0xAB, graphic=pp.LUTE_GRAPHIC)],
    )


@pytest.mark.asyncio
async def test_cursor_misses_do_not_consume_attempts(monkeypatch):
    """Two cursor misses then a steady cursor: the loop must still resolve the
    full ATTEMPTS_PER_RUN Peacemaking checks — the misses retried, they did not
    burn a slot. Under the old for-loop this resolved only 1."""
    ss = _ss()

    # Cursor sequence: miss, miss, then always-ok.
    seq = [_miss_cursor(), _miss_cursor()]
    target_calls = {"n": 0}

    async def fake_wait_for_target(_ctx, timeout=0.0):
        if seq:
            return seq.pop(0)
        return _ok_cursor()

    async def fake_target_object(_ctx, _cursor_id, serial):
        # Count only the creature-cursor answers (self serial = area peace).
        if serial == ss.serial:
            target_calls["n"] += 1

    monkeypatch.setattr(pp, "wait_for_target", fake_wait_for_target)
    monkeypatch.setattr(pp, "target_object", fake_target_object)

    result = await PracticePeacemaking().execute(_ctx(ss))

    # Exactly ATTEMPTS_PER_RUN creature-cursor answers (resolved checks),
    # despite the two leading misses.
    assert target_calls["n"] == ATTEMPTS_PER_RUN
    assert result.success is True
    assert f"/{ATTEMPTS_PER_RUN} calmed" in result.message


@pytest.mark.asyncio
async def test_persistent_cursor_miss_terminates(monkeypatch):
    """If the cursor NEVER arrives (wedged session), the loop must terminate via
    the bounded miss guard rather than spinning forever — and resolve zero."""
    ss = _ss()
    target_calls = {"n": 0}

    async def always_miss(_ctx, timeout=0.0):
        return _miss_cursor()

    async def fake_target_object(_ctx, _cursor_id, serial):
        if serial == ss.serial:
            target_calls["n"] += 1

    monkeypatch.setattr(pp, "wait_for_target", always_miss)
    monkeypatch.setattr(pp, "target_object", fake_target_object)

    import asyncio
    result = await asyncio.wait_for(
        PracticePeacemaking().execute(_ctx(ss)), timeout=5.0
    )

    # Never answered a creature cursor → zero resolved, message reflects 0/0.
    assert target_calls["n"] == 0
    assert result.success is True
    assert "0/0 calmed" in result.message


def test_loop_counts_only_resolved_attempts():
    """Source-shape guard: the loop must count resolved checks (while + a
    resolved counter), never a bare ``for attempt in range(ATTEMPTS_PER_RUN)``
    that lets a cursor miss eat an attempt slot."""
    import inspect

    src = inspect.getsource(PracticePeacemaking.execute)
    assert "while resolved < ATTEMPTS_PER_RUN" in src
    assert "resolved += 1" in src
    assert "for attempt in range(ATTEMPTS_PER_RUN)" not in src
