"""HeuristicModePolicy economic-sustainability lever (branch c) must key on the
EFFECTIVE mode the controller is running, not the fixed profession default.

Bug: branch (c) read ``actual`` (the persona's fixed profession default), so
once branch (d) rotated a low-risk miner onto a PURE skill-grind (magery/bard)
— which the sticky-continuation branch then sustains — branch (c) still judged
the stint as "mining" (in ``_GOLD_EARNING``). The moment that rotated
skill-grind's gold rate went flat (which is EXPECTED for a skill-grind, the
exact reason magery/bard are excluded from ``_GOLD_EARNING``) the gate fired and
steered the resident to ``travel``, yanking it off its stint and defeating the
balance lever. The fix evaluates the gate against the rotated (effective) mode.
"""
import pytest

from anima.planner.meta_controller import HeuristicModePolicy, LivingState


def _state(actual_mode, last_modes, *, gold_rate=0.0, weight=0.5, phase="mid"):
    # Healthy, safe, not overweight, mid phase, flat gold rate, non-trivial pack:
    # survival (a) and overweight (b) are suppressed and branch (c)'s own gates
    # (phase != early, gold_rate <= 0, weight >= 0.25) are satisfied — so the
    # ONLY thing deciding whether (c) fires is which mode it keys on.
    return LivingState(
        hp_frac=0.9, weight_frac=weight, gold=100, gold_rate_per_min=gold_rate,
        nearby_mobiles=0, danger_nearby=False, inventory_text="(empty)",
        session_minutes=30.0, phase=phase, last_modes=list(last_modes),
        actual_mode=actual_mode,
    )


class TestRotatedEconomyGate:
    @pytest.mark.asyncio
    async def test_rotated_magery_flat_gold_is_not_offloaded(self):
        # Miner rotated onto magery (a pure skill-grind, excluded from
        # _GOLD_EARNING) with a flat gold rate must KEEP the magery stint, NOT
        # be steered to travel/offload. last_modes[-1] == "magery" is the
        # in-progress rotation the controller is actually running.
        d = await HeuristicModePolicy().choose(
            _state("mining", ["mining", "mining", "magery"])
        )
        assert d.mode != "travel", d.rationale
        assert "economy" not in d.rationale
        assert d.mode == "magery", d.rationale

    @pytest.mark.asyncio
    async def test_rotated_bard_flat_gold_is_not_offloaded(self):
        # Same for the other pure skill-grind in the rotation.
        d = await HeuristicModePolicy().choose(
            _state("mining", ["mining", "mining", "bard"])
        )
        assert d.mode != "travel", d.rationale
        assert "economy" not in d.rationale
        assert d.mode == "bard", d.rationale

    @pytest.mark.asyncio
    async def test_un_rotated_mining_flat_gold_still_offloads(self):
        # Regression guard: with NO rotation in progress the effective mode
        # collapses to actual ("mining"), so a flat-gold direct-gold loop is
        # still relocated/offloaded exactly as before.
        d = await HeuristicModePolicy().choose(_state("mining", []))
        assert d.mode == "travel", d.rationale
        assert "economy" in d.rationale

    @pytest.mark.asyncio
    async def test_rotated_smithing_flat_gold_still_offloads(self):
        # A rotation onto the OTHER gold-earning loop (smithing) is still a
        # direct-gold loop, so a flat rate there is a real dead loop and the
        # gate must still fire — the fix only spares the pure skill-grinds.
        d = await HeuristicModePolicy().choose(
            _state("mining", ["mining", "mining", "smithing"])
        )
        assert d.mode == "travel", d.rationale
        assert "economy" in d.rationale
        assert "smithing" in d.rationale

    @pytest.mark.asyncio
    async def test_rotated_magery_positive_gold_unaffected(self):
        # Sanity: a healthy (positive) gold rate never trips branch (c) for any
        # mode, so the rotated magery stint continues regardless of the fix.
        d = await HeuristicModePolicy().choose(
            _state("mining", ["mining", "mining", "magery"], gold_rate=3.0)
        )
        assert d.mode == "magery", d.rationale
        assert d.mode != "travel"
