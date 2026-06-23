"""HeuristicModePolicy balance lever (branches d + e) must produce a SUSTAINED
rotated stint, not a one-tick blip.

Bug: branch (d) detected the over-grind streak against the fixed profession
``actual`` and rotated onto a new low-risk mode for exactly ONE decision, but
the early/mid day-phase default (branch e) then unconditionally snapped back to
``actual`` on the very next decision. The controller flip-flopped
``actual -> rotated -> actual -> rotated`` forever and never actually spent a
stint in the rotated trade, defeating the "a rounded resident varies work"
balance lever (docs/meta-controller.md §5 principle 2).

Fix: branch (e) persists an in-progress rotation (a low-risk productive last
choice that differs from ``actual``), and branch (d) re-rotates off whatever
low-risk mode is being repeated (not ``actual``), so the resident cycles
through the low-risk grinds instead of bouncing off one.
"""
import pytest

from anima.planner.meta_controller import HeuristicModePolicy, LivingState


def _state(actual_mode, last_modes, phase="early"):
    # Healthy, safe, light pack, profitable: branches (a)-(c) and the day-phase
    # economy offload are all suppressed so the balance lever / day-phase
    # default are the only live branches.
    return LivingState(
        hp_frac=0.9, weight_frac=0.2, gold=100, gold_rate_per_min=2.0,
        nearby_mobiles=0, danger_nearby=False, inventory_text="(empty)",
        session_minutes=10.0, phase=phase, last_modes=list(last_modes),
        actual_mode=actual_mode,
    )


class TestBalanceSticky:
    @pytest.mark.asyncio
    async def test_rotation_persists_next_decision(self):
        # First decision rotated mining -> smithing. The NEXT decision (history
        # now [mining, mining, smithing]) must KEEP smithing, not snap back to
        # mining. This is the core thrash bug.
        d = await HeuristicModePolicy().choose(
            _state("mining", ["mining", "mining", "smithing"])
        )
        assert d.mode == "smithing", d.rationale
        assert d.mode != "mining"

    @pytest.mark.asyncio
    async def test_full_cycle_varies_work(self):
        # Drive the policy like the controller does (feed back each choice) and
        # confirm the resident actually visits >1 distinct low-risk mode over a
        # run, instead of flip-flopping between exactly two.
        policy = HeuristicModePolicy()
        history = ["mining", "mining", "mining"]
        seen = set()
        for _ in range(12):
            d = await policy.choose(_state("mining", history))
            seen.add(d.mode)
            history.append(d.mode)
            history = history[-6:]
        # mining (default) + at least two rotated grinds = genuine variety,
        # which the old flip-flop ({mining, smithing} only) could never reach.
        assert "mining" in seen
        rotated = seen - {"mining"}
        assert len(rotated) >= 2, f"expected sustained variety, saw {seen}"
        assert rotated <= set(HeuristicModePolicy._LOW_RISK_PRODUCTIVE)

    @pytest.mark.asyncio
    async def test_re_rotation_off_a_rotated_mode(self):
        # Once a rotated mode (smithing) has itself been repeated for the whole
        # balance window, branch (d) must rotate OFF it again — even though
        # `actual` is still mining. The old `== actual` gate froze the resident
        # in the first rotated trade forever.
        order = HeuristicModePolicy._LOW_RISK_PRODUCTIVE
        expected = order[(order.index("smithing") + 1) % len(order)]
        d = await HeuristicModePolicy().choose(
            _state("mining", ["smithing", "smithing", "smithing"])
        )
        assert d.mode == expected, d.rationale
        assert "balance" in d.rationale

    @pytest.mark.asyncio
    async def test_non_grind_last_choice_falls_back_to_actual(self):
        # A non-low-risk last choice (combat) is NOT a balance rotation, so the
        # day-phase default must still snap to `actual` (unchanged behaviour).
        d = await HeuristicModePolicy().choose(
            _state("mining", ["mining", "combat"])
        )
        assert d.mode == "mining", d.rationale

    @pytest.mark.asyncio
    async def test_combat_persona_rotation_not_persisted(self):
        # A combat persona whose last choice is its own (high-risk) mode is not
        # a low-risk rotation; branch (e) must keep returning combat, never a
        # persisted low-risk grind.
        d = await HeuristicModePolicy().choose(
            _state("combat", ["combat", "combat"])
        )
        assert d.mode == "combat", d.rationale

    @pytest.mark.asyncio
    async def test_late_phase_unaffected(self):
        # The persist guard is early/mid only; the late phase still defaults to
        # socialize even mid-rotation (tidy-up day-phase).
        d = await HeuristicModePolicy().choose(
            _state("mining", ["mining", "mining", "smithing"], phase="late")
        )
        assert d.mode == "socialize", d.rationale
