"""HeuristicModePolicy economic-sustainability lever (branch c) must not treat
``combat`` like a direct-gold loop.

Bug: ``_GOLD_EARNING`` included ``combat``, so a warrior that is productively
fighting and accumulating looted gear — whose net-worth gold-rate is flat by
construction until it batch-sells — tripped branch (c) ("economy: gold-rate
flat -> relocate/offload") the instant its rate went <= 0 in mid/late phase
while carrying a non-trivial pack. That shadow-steered it OFF a winning fight,
the exact combat-uptime anti-pattern the balance-risk gate (branch d) already
excludes combat from. Combat earns *items*, not gold-per-minute, so a flat
gold rate is expected there — just like the skill-grinds the set excludes.
"""
import pytest

from anima.planner.meta_controller import HeuristicModePolicy, LivingState


def _flat_gold_state(actual_mode):
    # Healthy, safe, not overweight, NOT early phase, flat gold rate, carrying a
    # non-trivial pack: branches (a) survival, (b) overweight are suppressed and
    # branch (c)'s gates (phase != early, gold_rate <= 0, weight >= 0.25) are all
    # satisfied. No recent identical stints, so balance branch (d) cannot fire.
    return LivingState(
        hp_frac=0.9, weight_frac=0.5, gold=100, gold_rate_per_min=0.0,
        nearby_mobiles=0, danger_nearby=False, inventory_text="(empty)",
        session_minutes=30.0, phase="mid", last_modes=[],
        actual_mode=actual_mode,
    )


class TestCombatEconomyGate:
    @pytest.mark.asyncio
    async def test_combat_flat_gold_rate_does_not_offload(self):
        # A warrior with flat gold-rate (loot not yet sold) carrying gear must
        # NOT be steered to travel/offload — it stays in combat.
        d = await HeuristicModePolicy().choose(_flat_gold_state("combat"))
        assert d.mode == "combat", d.rationale
        assert d.mode != "travel"
        assert "economy" not in d.rationale

    @pytest.mark.asyncio
    async def test_combat_excluded_from_gold_earning_set(self):
        assert "combat" not in HeuristicModePolicy._GOLD_EARNING

    @pytest.mark.asyncio
    async def test_mining_flat_gold_rate_still_offloads(self):
        # Regression guard: a direct-gold loop (mining) with a flat rate and a
        # non-trivial pack IS still relocated/offloaded by branch (c).
        d = await HeuristicModePolicy().choose(_flat_gold_state("mining"))
        assert d.mode == "travel", d.rationale
        assert "economy" in d.rationale

    @pytest.mark.asyncio
    async def test_smithing_flat_gold_rate_still_offloads(self):
        # The other direct-gold loop stays in the set too.
        d = await HeuristicModePolicy().choose(_flat_gold_state("smithing"))
        assert d.mode == "travel", d.rationale
        assert "economy" in d.rationale

    @pytest.mark.asyncio
    async def test_combat_early_phase_unaffected(self):
        # Sanity: even with combat counted, branch (c) never fires in the early
        # phase; the fix must keep the early-phase combat default intact.
        state = _flat_gold_state("combat")
        state.phase = "early"
        d = await HeuristicModePolicy().choose(state)
        assert d.mode == "combat", d.rationale
        assert d.mode != "travel"
