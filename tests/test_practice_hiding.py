"""Regression guard for the THIEF-STEALTH hiding loop's phantom-success path.

``practice_hiding`` shares its loop shape with the BARD ``practice_peacemaking``
loop (a ``resolved`` counter bounded by ``MAX_ATTEMPTS``), but it once returned
``success=True`` unconditionally — even when every ``use_skill(Hiding)`` landed
inside the 10s skill lockout so ``wait_for_journal`` timed out and the Hiding
CheckSkill rolled ZERO times. That writes a phantom win to the reward signal and
inflates the THIEF-STEALTH skill-rate metric for a run that practiced nothing.
These tests pin the fix (mirrors f1e95e0 for peacemaking): a zero-resolved run
is a retryable BLOCKED failure with no re-suggestion; a run that resolves at
least one Hiding roll is still a real success.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.practice_hiding as ph
from anima.procedures.practice_hiding import ATTEMPTS_PER_RUN, PracticeHiding


def _ss():
    hiding = SimpleNamespace(value=40.0)
    stealth = SimpleNamespace(value=40.0)
    skills = {ph.SKILL_HIDING: hiding, ph.SKILL_STEALTH: stealth}
    return SimpleNamespace(
        is_alive=True,
        serial=0x1,
        hidden=False,
        skills=SimpleNamespace(get=skills.get),
    )


def _ctx(ss):
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss))


@pytest.fixture(autouse=True)
def _patch_clock_and_io(monkeypatch):
    # Never touch the network or actually sleep through the ~10s lockouts.
    monkeypatch.setattr(ph, "use_skill", AsyncMock())
    monkeypatch.setattr(ph.asyncio, "sleep", AsyncMock())
    # Deterministic, fast-forwarding clocks so the loop's lockout math is
    # exercised without wall-clock waits. ``monotonic`` advances a hair per
    # call so ``lockout_end - monotonic()`` stays well-defined; ``time`` is a
    # constant (only used as a journal ``since`` floor, which the mocks ignore).
    clock = {"t": 1000.0}

    def _monotonic():
        clock["t"] += 0.001
        return clock["t"]

    monkeypatch.setattr(ph.time, "monotonic", _monotonic)
    monkeypatch.setattr(ph.time, "time", lambda: 5000.0)


@pytest.mark.asyncio
async def test_zero_resolved_run_is_a_retryable_failure(monkeypatch):
    """Every Hiding attempt misses its result line (in-lockout / wedged): the
    loop terminates via the bounded miss guard and reports a retryable FAILURE,
    not a phantom ``success=True``."""
    ss = _ss()

    # wait_for_journal never resolves — models a run stuck inside the lockout.
    monkeypatch.setattr(
        ph, "wait_for_journal",
        AsyncMock(return_value=SimpleNamespace(success=False, data={})),
    )

    import asyncio
    result = await asyncio.wait_for(
        PracticeHiding().execute(_ctx(ss)), timeout=5.0
    )

    # Nothing rolled → retryable BLOCKED failure, never a phantom success.
    assert result.success is False
    assert result.reason is ph.FailureReason.BLOCKED
    # No re-suggestion to chain straight back into the same stall.
    assert result.next_suggestion is None
    # No skill credited (the gates moved no skill value in this run).
    assert not result.skill_gains


@pytest.mark.asyncio
async def test_resolved_run_is_a_success(monkeypatch):
    """A run that resolves Hiding rolls (a "can't seem to hide" miss still
    ROLLS the CheckSkill) is a real success that re-suggests itself."""
    ss = _ss()

    # index==1 is the "can't seem to hide" outcome: rolled (resolved) but not
    # hidden, so _stealth_walk is never entered (keeps the mock surface small).
    monkeypatch.setattr(
        ph, "wait_for_journal",
        AsyncMock(return_value=SimpleNamespace(success=True, data={"index": 1})),
    )

    import asyncio
    result = await asyncio.wait_for(
        PracticeHiding().execute(_ctx(ss)), timeout=5.0
    )

    assert result.success is True
    assert result.next_suggestion == "practice_hiding"
    assert f"/{ATTEMPTS_PER_RUN} hidden" in result.message


def test_loop_counts_only_resolved_attempts():
    """Source-shape guard: the loop must gate its success on a ``resolved``
    counter, not return ``success=True`` unconditionally."""
    import inspect

    src = inspect.getsource(PracticeHiding.execute)
    # The zero-resolved guard must exist (mirrors practice_peacemaking).
    assert "if resolved == 0:" in src
    assert "resolved < ATTEMPTS_PER_RUN" in src
