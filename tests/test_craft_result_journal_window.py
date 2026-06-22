"""``_await_craft_result`` must classify the craft outcome from the WHOLE
journal (scoped by the ``journal_mark`` timestamp floor), not a fixed
``recent(count=8)`` tail.

The bug: the batch loop waits up to ~6s per attempt, and ambient journal
traffic (gump chatter, combat keepalive, speech) routinely pushes THIS
attempt's own result line past the last 8 entries. A ``tool_broke`` outcome
arrives ONLY on the journal (the tongs are deleted, so no CraftGump is
re-sent), so a windowed scan that misses it drops the run into the
``''``/``unknown`` branch — re-navigating with no tongs and returning a
generic BLOCKED instead of the MISSING_RESOURCE that routes the planner to
make_tools/buy a replacement. This test plants the ``worn out your tool!``
line as an OLD entry behind 10 ambient lines (all after the mark) so it falls
outside ``recent(count=8)`` but inside the full timestamp-floored scan.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from anima.procedures.craft_blacksmith import CraftBlacksmith


@dataclass
class _Entry:
    text: str
    timestamp: float


class _Social:
    """Mirror of perception.SocialState's journal/recent contract."""

    def __init__(self) -> None:
        self.journal: deque[_Entry] = deque(maxlen=200)

    def add(self, text: str, ts: float) -> None:
        self.journal.append(_Entry(text=text, timestamp=ts))

    def recent(self, count: int = 10) -> list[_Entry]:
        return list(self.journal)[-count:]


def _make_ctx(social: _Social):
    ss = SimpleNamespace(gumps={})  # tongs destroyed → no CraftGump re-sent
    return SimpleNamespace(
        perception=SimpleNamespace(self_state=ss, social=social),
        bus=None,  # exercise the sleep-poll path (no bus condition wait)
    )


@pytest.mark.asyncio
async def test_tool_broke_line_past_recent_window_is_classified():
    proc = CraftBlacksmith()
    social = _Social()

    journal_mark = time.time()
    t = journal_mark + 0.001

    # The real result line for THIS attempt — arrives first...
    social.add("You have worn out your tool!", t)
    # ...then 10 ambient lines (all after the mark) bury it past recent(8).
    for i in range(10):
        social.add(f"ambient chatter {i}", t + 0.001 * (i + 1))

    # Sanity: the buggy recent(count=8) window no longer contains it.
    assert all(
        "worn out" not in e.text for e in social.recent(count=8)
    ), "test precondition: tool-broke line must be outside the recent(8) tail"

    ctx = _make_ctx(social)
    outcome, _notice = await proc._await_craft_result(
        ctx, journal_mark, timeout=1.0
    )
    assert outcome == "tool_broke"


@pytest.mark.asyncio
async def test_stale_line_before_mark_is_ignored():
    """A result line from a PRIOR attempt (timestamp < mark) must not count."""
    proc = CraftBlacksmith()
    social = _Social()

    journal_mark = time.time()
    # A success line from the previous attempt, before this attempt's mark.
    social.add("You create the longsword.", journal_mark - 5.0)

    ctx = _make_ctx(social)
    # No gump, no in-window line → unresolved → empty outcome after timeout.
    outcome, _notice = await proc._await_craft_result(
        ctx, journal_mark, timeout=0.5
    )
    assert outcome == ""
