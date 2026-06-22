"""BandageSelf must not book a *failed* poison cure as a heal success.

can_start lets a near-full-HP agent run bandage_self ONLY while poisoned
(ServUO Bandage.cs spends the bandage resolve attempting a cure, not an HP
heal). That cure requires Healing >= 60 AND Anatomy >= 60, so a low-skill
starter can NEVER land it — yet the server sends 500969 ("You finish applying
the bandages.") first, so the finish pattern matches and the bandage resolves.
The old code fell through to success=True, +0 HP on every such attempt: the
bandage was consumed, the poison kept ticking, and a phantom win was written to
the reward signal. A resolved cure-bandage that neither cured (still poisoned)
nor healed any HP must be a retryable failure.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import anima.procedures.bandage_self as bs
from anima.procedures.base import FailureReason


def _matches_finish(line: str) -> tuple[int, str] | None:
    for i, pat in enumerate(bs._FINISH_PATTERNS):
        if re.compile(re.escape(pat), re.IGNORECASE).search(line):
            return i, line
    return None


def _ctx(*, hits, hits_max=100, is_poisoned=False):
    skills = {bs.SKILL_HEALING: SimpleNamespace(value=30.0)}
    ss = SimpleNamespace(
        is_alive=True, serial=0x1, hits=hits, hits_max=hits_max,
        is_poisoned=is_poisoned, skills=skills,
    )
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss))


def _wire(monkeypatch, finish_line, *, heal_to=None, cure=False):
    """Wire a present bandage and a wait_for_journal that, on finish, applies
    the server's effects: optionally bump HP (heal_to) and/or clear poison
    (cure) the way ServUO's 0x17 status push would."""
    monkeypatch.setattr(bs, "find_in_backpack",
                        lambda ctx, g: [SimpleNamespace(serial=0xBA)])

    async def fake_use(ctx, obj, tgt):
        return SimpleNamespace(success=True, message="ok")

    monkeypatch.setattr(bs, "use_on_object", fake_use)

    async def fake_wait(ctx, patterns, timeout=5.0, since=None):
        ss = ctx.perception.self_state
        if heal_to is not None:
            ss.hits = heal_to
        if cure:
            ss.is_poisoned = False
        m = _matches_finish(finish_line)
        if m is None:
            return SimpleNamespace(success=False, data={}, message="timeout")
        idx, text = m
        return SimpleNamespace(success=True, data={"index": idx, "text": text})

    monkeypatch.setattr(bs, "wait_for_journal", fake_wait)
    monkeypatch.setattr(bs.time, "time", lambda: 1000.0)


@pytest.mark.asyncio
async def test_failed_poison_cure_is_retryable_failure(monkeypatch):
    """Poisoned, full HP, cure does NOT land (still poisoned, no HP healed):
    the resolved bandage must be a retryable BLOCKED failure, not a phantom
    +0 HP success."""
    _wire(monkeypatch, "You finish applying the bandages.")  # no heal, no cure
    res = await bs.BandageSelf().execute(_ctx(hits=100, is_poisoned=True))
    assert res.success is False
    assert res.reason is FailureReason.BLOCKED
    assert "still poisoned" in res.message.lower()


@pytest.mark.asyncio
async def test_successful_poison_cure_is_success(monkeypatch):
    """Same setup but the cure LANDS (ss.is_poisoned cleared): success."""
    _wire(monkeypatch, "You finish applying the bandages.", cure=True)
    res = await bs.BandageSelf().execute(_ctx(hits=100, is_poisoned=True))
    assert res.success is True


@pytest.mark.asyncio
async def test_poisoned_but_hp_healed_is_success(monkeypatch):
    """Even if still poisoned, any HP actually restored is a real heal —
    fall through to success (the failed-cure branch only fires on healed==0)."""
    _wire(monkeypatch, "You finish applying the bandages.", heal_to=55)
    res = await bs.BandageSelf().execute(_ctx(hits=50, is_poisoned=True))
    assert res.success is True
    assert "+5 HP" in res.message


@pytest.mark.asyncio
async def test_unpoisoned_zero_heal_is_unaffected(monkeypatch):
    """A non-poison wounded heal that resolves with no HP gain (a failed
    Healing roll legitimately grants nothing) is still a success — the new
    branch is gated on poisoned_before and must not touch this path."""
    _wire(monkeypatch, "You finish applying the bandages.")  # no heal, no poison
    res = await bs.BandageSelf().execute(_ctx(hits=50, is_poisoned=False))
    assert res.success is True
    assert "+0 HP" in res.message
