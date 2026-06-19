"""Target selection for *beneficial* actions aimed at another mobile.

A beneficial action — a Heal/Greater Heal cast, a thrown bandage, a Bless —
is the mirror image of combat targeting and has the opposite friend/foe gate.
Combat (``combat_loop._find_target``) only ever touches ATTACKABLE/ENEMY/
MURDERER notoriety; a *beneficial* target must be the reverse: only an
INNOCENT (blue) or ALLY (green) mobile is a safe recipient. Healing or
blessing a gray/criminal/enemy mobile in UO either fails outright or, worse,
flags the caster criminal/aggressor — so a wrong-faction target here is a
real correctness (and survival) bug, not a cosmetic one.

``find_beneficial_target`` is a pure, side-effect-free selector that returns a
*serial* (not a ``MobileInfo``) so the self-vs-other decision is unambiguous
to callers — the single most common bug in beneficial targeting is sending
the cursor at a friendly mobile's serial when it was self that needed the
heal, or vice-versa. Contract:

  * Only INNOCENT / ALLY notoriety mobiles are eligible (friend/foe gate).
  * Dead bodies / ghosts (``MobileInfo.is_dead``) are never targeted.
  * Only *wounded* parties (known health bar, ``hits < hits_max``) count —
    a beneficial heal on a full-HP target is wasted and the server refuses it.
  * Self is always a candidate; an ally is preferred over self only when the
    ally is *strictly* more wounded (lower HP fraction). On a tie self wins,
    because self is in hand (no line-of-sight / range gamble).
  * Returns ``None`` when nobody (including self) needs healing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from anima.perception.enums import NotorietyFlag

if TYPE_CHECKING:
    from anima.core.context import AgentContext

# The only notorieties a beneficial action may safely target. Deliberately the
# complement of combat_loop.ATTACKABLE_NOTORIETY — healing a gray/criminal/
# enemy flags the caller. INVULNERABLE (yellow, e.g. town healers) is excluded:
# it never needs our help and the server ignores beneficial casts on it.
FRIENDLY_NOTORIETY = frozenset({NotorietyFlag.INNOCENT, NotorietyFlag.ALLY})

# How far to scan for a wounded ally. Beneficial range in UO is short; keep it
# tight so we never path across the map to heal a stranger mid-fight.
BENEFICIAL_RANGE = 8


def _hp_frac(hits_max: int, hits: int) -> float | None:
    """Fraction of HP remaining, or ``None`` when the health bar is unknown.

    ``hits_max == 0`` means we have never queried this mobile's health bar
    (the MobileInfo defaults) — we cannot tell whether it is wounded, so it is
    not a valid heal target and must be skipped, never assumed full or empty.
    """
    if hits_max <= 0:
        return None
    return hits / hits_max


def find_beneficial_target(ctx: AgentContext) -> int | None:
    """Pick the serial of the best recipient for a beneficial action.

    See module docstring for the full contract. Returns the chosen mobile's
    serial (possibly self's own serial), or ``None`` if nothing is woundable.
    """
    ss = ctx.perception.self_state

    # Self is the baseline candidate. Unknown self health bar -> not a target.
    best_serial: int | None = None
    best_frac: float | None = None
    self_frac = _hp_frac(getattr(ss, "hits_max", 0), getattr(ss, "hits", 0))
    if self_frac is not None and self_frac < 1.0:
        best_serial = ss.serial
        best_frac = self_frac

    for m in ctx.perception.world.nearby_mobiles(ss.x, ss.y, distance=BENEFICIAL_RANGE):
        # Never re-target self via the world list (it may or may not appear
        # there); self is handled above with the authoritative self_state.
        if m.serial == ss.serial:
            continue
        # Friend/foe gate — the whole point of this selector.
        if m.notoriety not in FRIENDLY_NOTORIETY:
            continue
        # A corpse/ghost can't be healed. `is True` so SimpleNamespace stubs
        # without the attr aren't spuriously treated as dead.
        if getattr(m, "is_dead", False) is True:
            continue
        frac = _hp_frac(getattr(m, "hits_max", 0), getattr(m, "hits", 0))
        if frac is None or frac >= 1.0:
            continue  # unknown bar, or unwounded -> nothing to heal
        # Prefer the *more* wounded. Strict `<` so self wins ties (in hand,
        # no range/LOS gamble) and the result is order-independent.
        if best_frac is None or frac < best_frac:
            best_serial = m.serial
            best_frac = frac

    return best_serial
