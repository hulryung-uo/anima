"""HeuristicModePolicy balance lever (branch d) must only rotate the low-risk
productive grinds — never force a high-risk combat (or medium-risk thief)
persona OFF its profession and onto low-yield, off-profession mining.

Bug: branch (d) gated on ``actual in MODES`` (every mode), so after 3 identical
``combat`` stints it rotated the warrior to ``mining`` (``_next_low_risk``
falls back to the first low-risk entry for a mode outside the rotation list) —
the combat-uptime anti-pattern the planner combat loop exists to avoid. The
balance rotation set is ``_LOW_RISK_PRODUCTIVE``; the gate must match it.
"""
import pytest

from anima.planner.meta_controller import HeuristicModePolicy, LivingState


def _state(actual_mode, last_modes):
    # Healthy, safe, light-pack, profitable, early phase: every survival /
    # overweight / economy / day-phase branch above (a-c) is suppressed so the
    # balance branch (d) is the only one that can fire.
    return LivingState(
        hp_frac=0.9, weight_frac=0.2, gold=100, gold_rate_per_min=2.0,
        nearby_mobiles=0, danger_nearby=False, inventory_text="(empty)",
        session_minutes=10.0, phase="early", last_modes=last_modes,
        actual_mode=actual_mode,
    )


class TestBalanceRiskGate:
    @pytest.mark.asyncio
    async def test_combat_persona_not_rotated_to_mining(self):
        # 3 combat stints in a row: the OLD gate rotated to mining. The fixed
        # gate must keep the warrior in combat (its day-phase default).
        d = await HeuristicModePolicy().choose(
            _state("combat", ["combat", "combat", "combat"])
        )
        assert d.mode == "combat", d.rationale
        assert d.mode != "mining"
        assert "balance" not in d.rationale

    @pytest.mark.asyncio
    async def test_thief_persona_not_rotated(self):
        # Medium-risk thief is also outside the low-risk rotation set.
        d = await HeuristicModePolicy().choose(
            _state("thief", ["thief", "thief", "thief"])
        )
        assert d.mode == "thief", d.rationale
        assert d.mode != "mining"

    @pytest.mark.asyncio
    async def test_low_risk_grind_still_rotates(self):
        # Regression guard: a low-risk productive grind (mining) is still
        # balance-rotated off itself after 3 stints (unchanged behaviour).
        d = await HeuristicModePolicy().choose(
            _state("mining", ["mining", "mining", "mining"])
        )
        assert d.mode != "mining"
        assert d.mode in HeuristicModePolicy._LOW_RISK_PRODUCTIVE
        assert "balance" in d.rationale

    @pytest.mark.asyncio
    async def test_smithing_grind_rotates_to_next_in_list(self):
        d = await HeuristicModePolicy().choose(
            _state("smithing", ["smithing", "smithing", "smithing"])
        )
        # deterministic next-in-list after smithing
        order = HeuristicModePolicy._LOW_RISK_PRODUCTIVE
        expected = order[(order.index("smithing") + 1) % len(order)]
        assert d.mode == expected
        assert "balance" in d.rationale

    @pytest.mark.asyncio
    async def test_combat_fewer_than_three_stints_unaffected(self):
        # Only two combat stints — branch (d) cannot fire regardless of gate.
        d = await HeuristicModePolicy().choose(
            _state("combat", ["combat", "combat"])
        )
        assert d.mode == "combat"
