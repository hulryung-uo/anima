"""Skill-cap-reached skip for the mage profession loop.

practice_magery is the only entry in PROFESSION_LOOPS["mage"], and the
profession loop is "first startable wins". If can_start ignores the skill
cap, a mage whose Magery AND Meditation are both at cap keeps casting Greater
Heal for the entire eval window with ZERO skill gain instead of yielding so
the planner can do something productive. These tests pin the skip — and pin
that it does NOT mis-fire while any rolled skill can still climb (or while the
server has not reported a cap yet).
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import anima.procedures.practice_magery as pm
from anima.procedures.practice_magery import PracticeMagery


def _skill(base, cap):
    # value carries cap-inflating bonuses; gain is measured off base, so the
    # cap test must read base, not value.
    return SimpleNamespace(value=base, base=base, cap=cap)


def _ctx(mag_base, mag_cap, med_base, med_cap):
    skills = {
        pm.SKILL_MAGERY: _skill(mag_base, mag_cap),
        pm.SKILL_MEDITATION: _skill(med_base, med_cap),
    }
    ss = SimpleNamespace(
        is_alive=True,
        serial=0x1,
        mana=50,
        skills=SimpleNamespace(get=skills.get),
    )
    return SimpleNamespace(perception=SimpleNamespace(self_state=ss))


def _patch_preconds(monkeypatch):
    # Isolate the cap gate: pretend the spellbook + reagents are fine.
    monkeypatch.setattr(PracticeMagery, "_has_spellbook", lambda self, ctx: True)
    monkeypatch.setattr(PracticeMagery, "_reagent_shortage", lambda self, ctx: [])


@pytest.mark.asyncio
async def test_skips_when_both_skills_capped(monkeypatch):
    _patch_preconds(monkeypatch)
    proc = PracticeMagery()
    ctx = _ctx(mag_base=100.0, mag_cap=100.0, med_base=100.0, med_cap=100.0)
    assert await proc.can_start(ctx) is False


@pytest.mark.asyncio
async def test_runs_when_meditation_can_still_gain(monkeypatch):
    # Magery capped but Meditation still has headroom — the loop rolls
    # Meditation while regenerating mana, so it must NOT be skipped.
    _patch_preconds(monkeypatch)
    proc = PracticeMagery()
    ctx = _ctx(mag_base=100.0, mag_cap=100.0, med_base=42.0, med_cap=100.0)
    assert await proc.can_start(ctx) is True


@pytest.mark.asyncio
async def test_runs_when_magery_can_still_gain(monkeypatch):
    _patch_preconds(monkeypatch)
    proc = PracticeMagery()
    ctx = _ctx(mag_base=35.0, mag_cap=100.0, med_base=100.0, med_cap=100.0)
    assert await proc.can_start(ctx) is True


@pytest.mark.asyncio
async def test_unknown_cap_is_not_treated_as_capped(monkeypatch):
    # cap == 0.0 is the default before the server's 0x3A skill list arrives.
    # A missing cap must never masquerade as "capped" (would wrongly skip a
    # fresh low-skill mage who has every reason to grind).
    _patch_preconds(monkeypatch)
    proc = PracticeMagery()
    ctx = _ctx(mag_base=35.0, mag_cap=0.0, med_base=50.0, med_cap=0.0)
    assert await proc.can_start(ctx) is True


@pytest.mark.asyncio
async def test_capped_skip_falls_through_to_run_otherwise(monkeypatch):
    # Sanity: with caps cleared the same context starts normally — proving the
    # skip is the cap gate and not some other precondition.
    _patch_preconds(monkeypatch)
    proc = PracticeMagery()
    capped = _ctx(100.0, 100.0, 100.0, 100.0)
    not_capped = _ctx(100.0, 120.0, 100.0, 120.0)
    assert await proc.can_start(capped) is False
    assert await proc.can_start(not_capped) is True
