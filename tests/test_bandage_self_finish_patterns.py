"""BandageSelf must recognise EVERY ServUO bandage-finish line.

The bug: ``_FINISH_PATTERNS`` carried ``"barely manage"`` for the failed/low
heal outcome, but ServUO's Bandage.cs emits 500968 *"You apply the bandages,
but they barely help."* — "barely manage" appears in NO bandage cliloc (only
resurrection/stat-inspection strings). So the single most common low-skill
finish line (and 500967 "You heal what little damage your patient had.") never
matched: ``wait_for_journal`` burned its full 12 s timeout and ``execute`` fell
through to a phantom ``success=True, +0 HP`` result. These tests exercise the
real pattern list (no asyncio.sleep is hit, but it is mocked defensively) by
matching a supplied finish line exactly the way ``wait_for_journal`` does.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import anima.procedures.bandage_self as bs
from anima.procedures.base import FailureReason


def _matches_finish(line: str) -> tuple[int, str] | None:
    """Mirror wait_for_journal._check over the REAL _FINISH_PATTERNS list."""
    for i, pat in enumerate(bs._FINISH_PATTERNS):
        if re.compile(re.escape(pat), re.IGNORECASE).search(line):
            return i, line
    return None


def _ctx(hits=50, hits_max=100):
    skills = {bs.SKILL_HEALING: SimpleNamespace(value=30.0)}
    ss = SimpleNamespace(
        is_alive=True, serial=0x1, hits=hits, hits_max=hits_max,
        is_poisoned=False, skills=skills,
    )
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss))


def _wire(monkeypatch, finish_line: str, *, heal_to: int | None = None):
    """Wire a present bandage, a stub targeter, and a wait_for_journal that
    resolves to whatever the real pattern list makes of ``finish_line``."""
    monkeypatch.setattr(bs, "find_in_backpack",
                        lambda ctx, g: [SimpleNamespace(serial=0xBA)])

    async def fake_use(ctx, obj, tgt):
        return SimpleNamespace(success=True, message="ok")

    monkeypatch.setattr(bs, "use_on_object", fake_use)

    async def fake_wait(ctx, patterns, timeout=5.0, since=None):
        # apply the heal effect (HP bump) the way the server would, on finish
        if heal_to is not None:
            ctx.perception.self_state.hits = heal_to
        m = _matches_finish(finish_line)
        if m is None:
            return SimpleNamespace(success=False, data={}, message="timeout")
        idx, text = m
        return SimpleNamespace(success=True, data={"index": idx, "text": text})

    monkeypatch.setattr(bs, "wait_for_journal", fake_wait)

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(bs.time, "time", lambda: 1000.0)
    # defensive: nothing in execute should sleep, but pin it so a future edit
    # that adds a sleep can't make this test hang.
    monkeypatch.setattr("anima.actions.journal.asyncio.sleep", no_sleep, raising=False)


@pytest.mark.asyncio
async def test_barely_help_is_a_real_finish(monkeypatch):
    """500968 must be detected as a finished heal (was the regression)."""
    _wire(monkeypatch, "You apply the bandages, but they barely help.", heal_to=53)
    res = await bs.BandageSelf().execute(_ctx(hits=50))
    assert res.success is True
    assert res.reason is None
    assert "+3 HP" in res.message  # the heal was credited, not a phantom +0


@pytest.mark.asyncio
async def test_little_damage_is_a_real_finish(monkeypatch):
    """500967 'You heal what little damage your patient had.' resolves too."""
    _wire(monkeypatch, "You heal what little damage your patient had.", heal_to=51)
    res = await bs.BandageSelf().execute(_ctx(hits=50))
    assert res.success is True


@pytest.mark.asyncio
async def test_finish_applying_still_resolves(monkeypatch):
    """Baseline 500969 path unchanged."""
    _wire(monkeypatch, "You finish applying the bandages.", heal_to=70)
    res = await bs.BandageSelf().execute(_ctx(hits=50))
    assert res.success is True
    assert "+20 HP" in res.message


@pytest.mark.asyncio
async def test_not_damaged_is_refusal_by_text_not_index(monkeypatch):
    """500955 must still be a WRONG_LOCATION refusal — detected by matched
    text, so the new patterns can't shift it off a hardcoded index."""
    _wire(monkeypatch, "That being is not damaged!")
    res = await bs.BandageSelf().execute(_ctx(hits=99))
    assert res.success is False
    assert res.reason is FailureReason.WRONG_LOCATION


@pytest.mark.asyncio
async def test_timeout_is_retryable_failure_not_phantom_success(monkeypatch):
    """A bandage that never resolves (wait_for_journal times out) must NOT be
    booked as a heal success.

    ``_wire`` returns ``success=False`` from the stub wait_for_journal whenever
    the supplied line matches no finish pattern — i.e. a true timeout. The agent
    is wounded (50/100) and its HP never moved, so the old fall-through booked a
    phantom ``success=True, +0 HP`` win that suppressed any retry. The procedure
    must instead surface a retryable INTERRUPTED failure.
    """
    _wire(monkeypatch, "some unrelated journal noise that matches no pattern")
    res = await bs.BandageSelf().execute(_ctx(hits=50))
    assert res.success is False
    assert res.reason is FailureReason.INTERRUPTED
    # and it must not claim a heal/skill gain it never earned
    assert not res.skill_gains


def test_obsolete_pattern_is_gone():
    """Guard against re-introducing the cliloc-less 'barely manage' string."""
    assert "barely manage" not in bs._FINISH_PATTERNS
    assert "barely help" in bs._FINISH_PATTERNS
    assert "little damage" in bs._FINISH_PATTERNS
