"""Regression guard for the BARD peacemaking loop's lockout accounting.

The Peacemaking skill lockout (~10s) starts server-side the instant the skill
is USED. The seconds the loop then spends waiting on the target cursor and the
result journal line are ALREADY part of that same window. The old code filled a
fresh full ``SKILL_USE_COOLDOWN_S`` window measured from AFTER the result wait,
double-counting that wait — up to ~3s of over-sleep per resolved attempt, ~6-9s
wasted per ~30s run that could have gone to more Musicianship plays / the next
Peacemaking check.

These tests pin the fix: ``_play_through_lockout`` is handed an absolute
``time.monotonic()`` deadline anchored to the ``use_skill`` instant, so the
result-wait elapsed time REDUCES the remaining fill rather than stacking on top
of it (and is a clean no-op once the deadline has already passed).
"""
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.practice_peacemaking as pp
from anima.procedures.practice_peacemaking import (
    ATTEMPTS_PER_RUN,
    PracticePeacemaking,
)
from anima.actions.skills import SKILL_USE_COOLDOWN_S


class _Clock:
    """Deterministic monotonic clock: advances only when we tell it to."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


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


def _ctx(ss):
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss))


@pytest.fixture(autouse=True)
def _patch_common(monkeypatch):
    # Warmed-up path: the instrument-prompt journal check never fires.
    monkeypatch.setattr(
        pp, "wait_for_journal",
        AsyncMock(return_value=SimpleNamespace(success=False, data={})),
    )
    monkeypatch.setattr(pp, "use_skill", AsyncMock())
    monkeypatch.setattr(pp, "target_object", AsyncMock())
    monkeypatch.setattr(pp, "wait_for_target", AsyncMock(return_value=_ok_cursor()))
    monkeypatch.setattr(
        pp, "find_in_backpack",
        lambda _ctx, _g: [SimpleNamespace(serial=0xAB, graphic=pp.LUTE_GRAPHIC)],
    )


def test_signature_takes_a_deadline_not_a_duration():
    """Source-shape guard: the fill helper must accept an absolute monotonic
    deadline (``lockout_end``) and loop against ``time.monotonic()`` — never a
    bare duration measured from when it is called (which double-counts the
    cursor/result wait already inside the lockout window)."""
    sig = inspect.signature(pp._play_through_lockout)
    assert "lockout_end" in sig.parameters
    src = inspect.getsource(pp._play_through_lockout)
    assert "time.monotonic()" in src
    # The duration-from-now anti-pattern must be gone.
    assert "get_event_loop().time() + duration" not in src


@pytest.mark.asyncio
async def test_result_wait_is_subtracted_from_the_lockout_fill(monkeypatch):
    """On a resolved attempt, the deadline handed to _play_through_lockout is
    anchored to the use_skill instant — so a slow (~3s) cursor+result wait
    REDUCES the remaining fill instead of starting a fresh full window.

    We capture every deadline _play_through_lockout receives and assert it is
    the pre-use_skill clock + (SKILL_USE_COOLDOWN_S + 0.5), regardless of how
    much wall time the cursor/result waits consumed in between."""
    ss = _ss()
    clock = _Clock()
    monkeypatch.setattr(pp.time, "monotonic", clock)

    captured: list[float] = []

    async def fake_fill(_ctx, _serial, lockout_end):
        captured.append(lockout_end)
        return 0  # plays

    monkeypatch.setattr(pp, "_play_through_lockout", fake_fill)

    # Model the cursor + result waits each burning real (monotonic) time, the
    # way wait_for_target / wait_for_journal would in production.
    async def slow_cursor(_ctx, timeout=0.0):
        clock.advance(1.5)  # cursor latency inside the lockout window
        return _ok_cursor()

    async def slow_result(_ctx, _patterns, timeout=0.0, since=0.0):
        clock.advance(1.5)  # result-line latency, also inside the window
        return SimpleNamespace(success=False, data={})

    monkeypatch.setattr(pp, "wait_for_target", slow_cursor)
    monkeypatch.setattr(pp, "wait_for_journal", slow_result)

    # Anchor for the FIRST attempt is the clock value at loop entry.
    first_anchor = clock.t

    await PracticePeacemaking().execute(_ctx(ss))

    assert len(captured) == ATTEMPTS_PER_RUN
    expected_window = SKILL_USE_COOLDOWN_S + 0.5
    # The deadline for the first attempt must be anchored to use_skill (loop
    # entry), NOT to the post-result-wait clock. Under the old duration-based
    # code the effective deadline would have been (anchor + 3.0 waits +
    # window); anchoring drops those 3.0s of double-counted wait.
    assert captured[0] == pytest.approx(first_anchor + expected_window)
    # And the deadline must be BEFORE the clock value reached by the time the
    # fill is actually invoked (the result wait already ate into the window).
    # i.e. the over-wait the old code introduced is provably removed.
    assert captured[0] < clock.t + expected_window


@pytest.mark.asyncio
async def test_already_elapsed_deadline_makes_fill_a_noop(monkeypatch):
    """If the cursor/result wait already consumed the whole lockout, the real
    _play_through_lockout must take ZERO plays — never sleep a fresh window."""
    clock = _Clock()
    monkeypatch.setattr(pp.time, "monotonic", clock)
    sleeps: list[float] = []

    async def fake_sleep(dt):
        sleeps.append(dt)

    monkeypatch.setattr(pp.asyncio, "sleep", fake_sleep)

    ctx = SimpleNamespace(conn=SimpleNamespace(send_packet=AsyncMock()))
    # Deadline already in the past relative to the clock → no plays, no sleeps.
    plays = await pp._play_through_lockout(ctx, 0xAB, clock.t - 1.0)
    assert plays == 0
    assert sleeps == []
    ctx.conn.send_packet.assert_not_called()
