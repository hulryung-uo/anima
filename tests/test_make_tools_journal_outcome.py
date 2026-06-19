"""make_tools._journal_craft_outcome — only read THIS craft's journal lines.

Regression guard for the phantom-success bug: make_tools re-suggests itself and
runs back-to-back, so a prior attempt's "You create ..." (or "you fail") line is
still in the recent journal window. The old fallback scanned recent(count=5)
with no timestamp floor, so a stale success line was credited to a craft that
actually failed — clearing the fail counter and skipping the give-up / buy
circuit breaker so a low-Tinkering smith looped forever on a craft it can't land.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from anima.procedures.make_tools import _journal_craft_outcome


@dataclass
class _Entry:
    text: str
    timestamp: float = field(default_factory=time.time)


def test_stale_create_before_craft_start_is_ignored():
    """A 'you create' from a PRIOR attempt (before craft_start) is not a success."""
    t0 = 1000.0
    journal = [_Entry("You create the item and place it in your backpack.", t0 - 5.0)]
    # The current craft pressed its button at t0; nothing resolved since.
    assert _journal_craft_outcome(journal, since=t0) is None


def test_fresh_create_after_craft_start_is_success():
    t0 = 1000.0
    journal = [_Entry("You create the item and place it in your backpack.", t0 + 1.0)]
    assert _journal_craft_outcome(journal, since=t0) == "create"


def test_stale_create_does_not_mask_a_fresh_fail():
    """The real failure mode: a stale success + a fresh genuine failure.

    The current (failing) craft must classify as 'fail' off its OWN line, not be
    rescued to a phantom 'create' by the leftover success of the prior attempt.
    """
    t0 = 1000.0
    journal = [
        _Entry("You create the item and place it in your backpack.", t0 - 3.0),
        _Entry("You fail to create the item.", t0 + 1.0),
    ]
    assert _journal_craft_outcome(journal, since=t0) == "fail"


def test_fresh_fail_after_craft_start():
    t0 = 1000.0
    journal = [_Entry("You don't have the resources to make that.", t0 + 0.5)]
    assert _journal_craft_outcome(journal, since=t0) == "fail"


def test_no_relevant_line_returns_none():
    t0 = 1000.0
    journal = [_Entry("The Earl of Twilight wanders by.", t0 + 1.0)]
    assert _journal_craft_outcome(journal, since=t0) is None


def test_create_wins_when_it_precedes_a_later_unrelated_line():
    t0 = 1000.0
    journal = [
        _Entry("You create the item and place it in your backpack.", t0 + 0.2),
        _Entry("Some unrelated chatter.", t0 + 0.4),
    ]
    assert _journal_craft_outcome(journal, since=t0) == "create"
