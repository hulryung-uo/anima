"""Modes — grid behaviours exposed as selectable tools for the meta-controller.

A `Mode` is one coherent way to spend time: a profession loop (mining,
smithing, magery, bard, combat) plus the "living" modes a rounded resident
needs (rest, socialize, travel). Each mode wraps an ordered tuple of
procedure names — the same strings the planner's `PROFESSION_LOOPS` uses —
so the meta-controller can pick *which* mode to run without the planner
needing to know anything new.

This module is pure data (no anima imports) so it can be imported anywhere
without circular-dependency risk. See `docs/meta-controller.md`.

NOTE (P0 shadow stage): the planner does NOT yet consume `active_mode`.
The meta-controller only *logs* what it would pick. `MODES` metadata is
consumed by the controller's prompt; the `loop` tuples are wired into
procedure selection in P1.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Mode:
    """One selectable way to spend time.

    loop:     ordered procedure names; first startable wins (P1). An empty
              loop means "fall through to the planner's default ladder"
              (the generic mining/recovery chain) — used for `mining` and
              for living modes whose behaviour is driven by planner
              priorities rather than a dedicated procedure.
    good_for: one-line description fed to the controller's prompt (derived
              from the grid elites' hypotheses).
    needs:    precondition keywords, advisory for the controller's reasoning.
    risk:     open-world danger of running this mode: low | medium | high.
    """

    name: str
    loop: tuple[str, ...]
    good_for: str
    needs: tuple[str, ...] = field(default_factory=tuple)
    risk: str = "low"


# The grid's profession loops, absorbed and annotated, plus the living modes.
# Profession loops mirror anima/planner/planner.py:PROFESSION_LOOPS exactly so
# behaviour is unchanged when P1 wires `loop` into selection.
MODES: dict[str, Mode] = {
    "mining": Mode(
        name="mining",
        loop=(),  # miner falls through to the generic mine→smelt→sell ladder
        good_for="skill + gold via ore → ingot → sell; the universal fallback",
        needs=("pickaxe",),
        risk="low",
    ),
    "smithing": Mode(
        name="smithing",
        loop=("craft_blacksmith", "smelt_ore", "sell_to_vendor"),
        good_for="craft weapons/armor for gold; stationary at a forge",
        needs=("forge", "ingots", "tongs"),
        risk="low",
    ),
    "magery": Mode(
        name="magery",
        loop=("practice_magery",),
        good_for="fastest skill gain (Magery via self-cast + meditation)",
        needs=("reagents", "mana"),
        risk="low",
    ),
    "bard": Mode(
        name="bard",
        loop=("practice_peacemaking", "practice_music"),
        good_for="Musicianship/Peacemaking via instrument plays; pacifies foes",
        needs=("instrument",),
        risk="low",
    ),
    "combat": Mode(
        name="combat",
        loop=("bandage_self", "hunt_nearby"),
        good_for="Swords/Tactics/Anatomy/Healing + gold from kills",
        needs=("weapon", "bandages"),
        risk="high",
    ),
    "thief": Mode(
        name="thief",
        loop=("practice_hiding",),
        good_for="Hiding/Stealth grind; needs no items",
        needs=(),
        risk="medium",
    ),
    # --- living modes (behaviour driven by planner priorities in P1/P2) ---
    "rest": Mode(
        name="rest",
        loop=("bandage_self",),
        good_for="recover HP / consumables before resuming; low-effort uptime",
        needs=(),
        risk="low",
    ),
    "socialize": Mode(
        name="socialize",
        loop=(),  # responds to pending speech (planner social priority)
        good_for="answer pending speech, build presence — value is in RESPONSES",
        needs=(),
        risk="low",
    ),
    "travel": Mode(
        name="travel",
        loop=(),  # relocate / seek safer or richer ground
        good_for="relocate to safer or more productive ground",
        needs=(),
        risk="medium",
    ),
}


# Map a persona's fixed profession to its default mode, so the controller can
# compare its (shadow) choice against what the planner is actually running.
_PROFESSION_TO_MODE: dict[str, str] = {
    "mage": "magery",
    "bard": "bard",
    "thief": "thief",
    "adventurer": "combat",
    "blacksmith": "smithing",
    "miner": "mining",
    "": "mining",
}


def default_mode_for_profession(profession: str | None) -> str:
    """The mode the planner is effectively running for a given profession."""
    return _PROFESSION_TO_MODE.get(profession or "", "mining")
