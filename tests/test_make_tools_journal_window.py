"""make_tools must scan the WHOLE journal for its craft result, not a 5-tail.

Regression guard for the residual half of the phantom-outcome bug. Commit
969fba2 added a ``craft_start`` timestamp floor INSIDE ``_journal_craft_outcome``
so a stale prior-attempt line can't be misread — but the call site still handed
it only ``social.recent(count=5)``. The craft waits ~4s, and ambient journal
traffic (gump craft chatter, combat keepalive, other mobiles) routinely pushes
THIS craft's own "You create ..." line past the last five entries. Truncating
to a 5-tail BEFORE the timestamp gate runs therefore drops a genuine success,
which mis-books the craft as a failure and ticks ``_make_tools_fails`` toward the
5-strike give-up that makes the planner abandon crafting and buy instead.

These tests pin the contract that the timestamp floor — not an arbitrary count —
decides relevance: a fresh result line buried behind >5 newer (but still
post-craft) entries must still be found.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from anima.procedures.make_tools import _journal_craft_outcome


@dataclass
class _Entry:
    text: str
    timestamp: float


def _recent(journal, count: int) -> list:
    """Mirror SocialState.recent(count) — return the last ``count`` entries."""
    return list(journal)[-count:]


def test_fresh_create_buried_behind_five_newer_lines_is_found():
    """The success line lands, then >5 unrelated lines arrive within the wait.

    Scanning the full journal (timestamp-gated) finds the create; the old
    recent(count=5) tail truncates the create away and would miss it.
    """
    t0 = 1000.0
    journal = deque(
        [
            _Entry("You create the item and place it in your backpack.", t0 + 0.1),
            _Entry("A chicken wanders by.", t0 + 0.5),
            _Entry("The wind howls.", t0 + 0.7),
            _Entry("You feel yourself attuned to nature.", t0 + 0.9),
            _Entry("Something stirs nearby.", t0 + 1.1),
            _Entry("A bird chirps.", t0 + 1.3),
        ]
    )

    # New behaviour: whole journal, timestamp-gated → success is detected.
    assert _journal_craft_outcome(journal, since=t0) == "create"

    # Demonstrate the defect the fix removes: the old 5-tail drops the create
    # line (it is the 6th-from-last entry), so the gate never sees it.
    assert _journal_craft_outcome(_recent(journal, 5), since=t0) is None


def test_fresh_fail_buried_behind_five_newer_lines_is_found():
    """Symmetric: a genuine failure buried behind newer post-craft chatter."""
    t0 = 2000.0
    journal = deque(
        [
            _Entry("You fail to create the item.", t0 + 0.1),
            _Entry("A rabbit hops away.", t0 + 0.4),
            _Entry("Leaves rustle.", t0 + 0.6),
            _Entry("A distant bell tolls.", t0 + 0.8),
            _Entry("You hear footsteps.", t0 + 1.0),
            _Entry("A cat meows.", t0 + 1.2),
        ]
    )

    assert _journal_craft_outcome(journal, since=t0) == "fail"
    # Old 5-tail would have truncated the fail line and returned None.
    assert _journal_craft_outcome(_recent(journal, 5), since=t0) is None


def test_stale_create_before_craft_start_still_ignored_on_full_journal():
    """Scanning the whole journal must NOT resurrect the prior-attempt bug.

    A leftover success from a previous back-to-back craft (timestamp before
    craft_start) is still correctly ignored even though it is now visible.
    """
    t0 = 3000.0
    journal = deque(
        [
            _Entry("You create the item and place it in your backpack.", t0 - 5.0),
            _Entry("Idle chatter one.", t0 - 4.0),
            _Entry("Idle chatter two.", t0 - 3.0),
        ]
    )
    # Nothing resolved since the current craft pressed its button.
    assert _journal_craft_outcome(journal, since=t0) is None


def test_full_journal_still_picks_create_over_a_later_unrelated_line():
    t0 = 4000.0
    journal = deque(
        [
            _Entry("You create the item and place it in your backpack.", t0 + 0.2),
            _Entry("Some unrelated chatter.", t0 + 0.4),
            _Entry("More chatter.", t0 + 0.6),
            _Entry("Even more chatter.", t0 + 0.8),
            _Entry("Yet more chatter.", t0 + 1.0),
            _Entry("Still more chatter.", t0 + 1.2),
        ]
    )
    assert _journal_craft_outcome(journal, since=t0) == "create"
