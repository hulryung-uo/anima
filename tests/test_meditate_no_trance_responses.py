"""meditate() must short-circuit on the no-trance Meditation responses.

ServUO Meditation.OnUse enters a trance — and the mana-regen boost — ONLY on
cliloc 501851 ("You enter a meditative trance."). Every other branch (501846
"You are at peace.", 502626 hands, 500135 armor, 501849 body) leaves
m.Meditating false, so mana never climbs. Before the fix the action only knew
about 501851 / "cannot focus", so any other line fell through to the mana
poll loop and spun for the FULL timeout (default 30s) before returning a
misleading success="Meditation window over". These tests pin the immediate,
correctly-classified returns.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.actions.skills as sk
from anima.actions.skills import meditate


def _ctx(journal_text: str, *, mana: int = 5, mana_max: int = 50):
    """A ctx with bus=None (so wait_for_journal uses its polling fallback) and
    a single pre-seeded journal line at timestamp 0 (always < `since`-now? no —
    we set the entry timestamp high so it is considered)."""
    entry = SimpleNamespace(timestamp=1e18, text=journal_text)
    social = SimpleNamespace(journal=[entry])
    ss = SimpleNamespace(serial=0x1, mana=mana, mana_max=mana_max)
    conn = SimpleNamespace(send_packet=AsyncMock())
    return SimpleNamespace(
        bus=None,
        conn=conn,
        perception=SimpleNamespace(self_state=ss, social=social),
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Any sleep in the action (or the journal polling fallback) is a no-op so
    # a regression that spins the loop would still be fast — but it would call
    # asyncio.sleep many times, which our assertions below catch.
    monkeypatch.setattr(sk.asyncio, "sleep", AsyncMock())


@pytest.mark.asyncio
async def test_at_peace_returns_success_without_polling(monkeypatch):
    # 501846 "You are at peace." — mana full, no trance. Must return success
    # immediately, NOT fall into the mana poll loop.
    ctx = _ctx("You are at peace.", mana=5, mana_max=50)

    poll_sleeps = {"n": 0}
    real_sleep = sk.asyncio.sleep

    async def counting_sleep(d):
        # The poll loop sleeps 1.0s per tick; count those to prove we did not
        # enter it. (journal fallback sleeps 0.1 but bus=None + pre-seeded
        # entry means _check() matches on the first pass with no sleep.)
        if d >= 1.0:
            poll_sleeps["n"] += 1
        return await real_sleep(d)

    monkeypatch.setattr(sk.asyncio, "sleep", counting_sleep)

    result = await meditate(ctx, target_pct=90.0, timeout=30.0)

    assert result.success is True
    assert "peace" in result.message.lower()
    assert poll_sleeps["n"] == 0  # never entered the 1s-per-tick poll loop


@pytest.mark.asyncio
async def test_hands_blocked_returns_failure_immediately():
    # 502626 "Your hands must be free to cast spells or meditate." — blocked.
    ctx = _ctx("Your hands must be free to cast spells or meditate.")
    result = await meditate(ctx, target_pct=90.0, timeout=30.0)
    assert result.success is False
    assert "block" in result.message.lower()


@pytest.mark.asyncio
async def test_armor_blocked_returns_failure_immediately():
    # 500135 "Regenative forces cannot penetrate your armor!" — blocked.
    ctx = _ctx("Regenative forces cannot penetrate your armor!")
    result = await meditate(ctx, target_pct=90.0, timeout=30.0)
    assert result.success is False
    assert "block" in result.message.lower()


@pytest.mark.asyncio
async def test_body_weak_returns_failure_immediately():
    # 501849 "The mind is strong but the body is weak." — blocked.
    ctx = _ctx("The mind is strong but the body is weak.")
    result = await meditate(ctx, target_pct=90.0, timeout=30.0)
    assert result.success is False
    assert "block" in result.message.lower()


@pytest.mark.asyncio
async def test_cannot_focus_still_returns_failure():
    # 501850 "You cannot focus your concentration." — failed roll (regression
    # guard for the pre-existing branch).
    ctx = _ctx("You cannot focus your concentration.")
    result = await meditate(ctx, target_pct=90.0, timeout=30.0)
    assert result.success is False
    assert "focus" in result.message.lower()


@pytest.mark.asyncio
async def test_trance_then_mana_climb_still_succeeds(monkeypatch):
    # 501851 "You enter a meditative trance." — the real regen path. Mana
    # climbs across poll ticks and the action returns success once target_pct
    # is reached.
    ctx = _ctx("You enter a meditative trance.", mana=5, mana_max=50)
    ss = ctx.perception.self_state

    real_sleep = sk.asyncio.sleep

    async def climbing_sleep(d):
        if d >= 1.0:
            ss.mana = min(ss.mana_max, ss.mana + 25)
        return await real_sleep(d)

    monkeypatch.setattr(sk.asyncio, "sleep", climbing_sleep)

    result = await meditate(ctx, target_pct=90.0, timeout=30.0)

    assert result.success is True
    assert (ss.mana / ss.mana_max) * 100.0 >= 90.0
