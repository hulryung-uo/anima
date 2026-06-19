"""A poisoned agent must apply a (cure) bandage even at high HP.

In ServUO a bandage applied while poisoned spends its resolve attempting a
cure; poison ticks keep draining HP regardless of the bar reading. The
combat-loop self-bandage previously gated *only* on HP-percent, so a poisoned
agent above the bandage trigger (e.g. 80%% HP) would never apply a bandage and
would tick to death. ``_maybe_bandage`` now lets an active poison force a
bandage independent of the HP gate, while still honouring the reapply cooldown.
"""
from types import SimpleNamespace

import pytest

import anima.procedures.combat_loop as cl
from anima.procedures.combat_loop import BANDAGE_REAPPLY_S, _maybe_bandage


def _ctx(hp_pct=100.0, hits_max=100, is_poisoned=False):
    ss = SimpleNamespace(
        hits_max=hits_max,
        hp_percent=hp_pct,
        serial=0x1,
        is_poisoned=is_poisoned,
    )
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss),
        blackboard={},
    )


def _wire(monkeypatch):
    """Wire a present bandage and a recording use_on_object; return the log."""
    used_calls = []
    monkeypatch.setattr(
        cl, "find_in_backpack",
        lambda ctx, g: [SimpleNamespace(serial=0xBA)],
    )

    async def fake_use(ctx, obj, tgt, timeout=2.0):
        used_calls.append((obj, tgt))
        return SimpleNamespace(success=True, message="ok")

    monkeypatch.setattr(cl, "use_on_object", fake_use)
    return used_calls


@pytest.mark.asyncio
async def test_poisoned_at_high_hp_bandages(monkeypatch):
    """80%% HP would normally skip (80 >= 85 baseline trigger? no — but a fresh
    high-HP read keeps the baseline 85 trigger, so 80 < 85 would heal anyway).
    Use 95%% HP where the HP gate alone clearly skips, and assert poison still
    forces the cure bandage."""
    used_calls = _wire(monkeypatch)
    ctx = _ctx(hp_pct=95.0, is_poisoned=True)
    monkeypatch.setattr(cl.time, "monotonic", lambda: 1000.0)

    await _maybe_bandage(ctx)

    # 95 >= BANDAGE_HP_PCT (85) → the HP gate alone would skip; poison overrides.
    assert used_calls == [(0xBA, 0x1)]
    assert ctx.blackboard["_bandage_last_ts"] == 1000.0


@pytest.mark.asyncio
async def test_not_poisoned_at_high_hp_skips(monkeypatch):
    """Control: same high HP, NOT poisoned → no bandage (baseline preserved)."""
    used_calls = _wire(monkeypatch)
    ctx = _ctx(hp_pct=95.0, is_poisoned=False)
    monkeypatch.setattr(cl.time, "monotonic", lambda: 1000.0)

    await _maybe_bandage(ctx)

    assert used_calls == []
    assert "_bandage_last_ts" not in ctx.blackboard


@pytest.mark.asyncio
async def test_poison_respects_reapply_cooldown(monkeypatch):
    """Poison forces a bandage but must not spam inside the reapply window —
    a cure attempt occupies the same ~8s bandage timer."""
    used_calls = _wire(monkeypatch)
    ctx = _ctx(hp_pct=95.0, is_poisoned=True)

    monkeypatch.setattr(cl.time, "monotonic", lambda: 1000.0)
    await _maybe_bandage(ctx)
    assert used_calls == [(0xBA, 0x1)]

    # Still inside the reapply cooldown → second poisoned tick is a no-op.
    monkeypatch.setattr(
        cl.time, "monotonic", lambda: 1000.0 + BANDAGE_REAPPLY_S - 0.5
    )
    await _maybe_bandage(ctx)
    assert used_calls == [(0xBA, 0x1)]

    # Past the cooldown → the cure bandage re-arms while still poisoned.
    monkeypatch.setattr(
        cl.time, "monotonic", lambda: 1000.0 + BANDAGE_REAPPLY_S + 0.1
    )
    await _maybe_bandage(ctx)
    assert used_calls == [(0xBA, 0x1), (0xBA, 0x1)]


@pytest.mark.asyncio
async def test_poison_no_op_with_unknown_hp(monkeypatch):
    """hits_max <= 0 (no known health bar) is still a clean no-op even when the
    poison flag is set — we never fire a bandage on an un-synced mobile."""
    used_calls = _wire(monkeypatch)
    ctx = _ctx(hp_pct=100.0, hits_max=0, is_poisoned=True)
    monkeypatch.setattr(cl.time, "monotonic", lambda: 1000.0)

    await _maybe_bandage(ctx)

    assert used_calls == []


@pytest.mark.asyncio
async def test_missing_is_poisoned_attr_defaults_false(monkeypatch):
    """Defensive: a self_state mock lacking is_poisoned must read as not
    poisoned (getattr default), preserving pure HP-gated behavior."""
    used_calls = _wire(monkeypatch)
    ss = SimpleNamespace(hits_max=100, hp_percent=95.0, serial=0x1)
    ctx = SimpleNamespace(perception=SimpleNamespace(self_state=ss), blackboard={})
    monkeypatch.setattr(cl.time, "monotonic", lambda: 1000.0)

    await _maybe_bandage(ctx)

    assert used_calls == []
