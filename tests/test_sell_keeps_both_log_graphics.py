"""SellToNpc must protect BOTH log stack graphics from being sold.

UO stores logs under two graphics depending on stack size (0x1BDD and
0x1BE0); every other module (lumber, make_boards, banking, state encoder)
treats the pair {0x1BDD, 0x1BE0} as "logs". The vendor sell-protection set
previously listed only 0x1BDD, so the larger-stack 0x1BE0 logs were happily
sold to vendors — starving the carpentry/board-making loop of raw material.
"""

from __future__ import annotations

from anima.skills.gathering.lumber import LOG_GRAPHICS
from anima.skills.trade.vendor import KEEP_GRAPHICS

# Both stack-size graphics UO uses for logs.
LOG_0x1BDD = 0x1BDD
LOG_0x1BE0 = 0x1BE0


def test_both_log_graphics_are_kept_from_selling() -> None:
    assert LOG_0x1BDD in KEEP_GRAPHICS
    # Regression: 0x1BE0 logs used to be missing and got sold away.
    assert LOG_0x1BE0 in KEEP_GRAPHICS


def test_keep_set_covers_full_log_graphic_family() -> None:
    # The vendor protection set must cover the canonical log family used by
    # the gathering/crafting skills, so nothing the agent needs for boards
    # leaks into the sell list.
    assert LOG_GRAPHICS == {LOG_0x1BDD, LOG_0x1BE0}
    assert LOG_GRAPHICS <= KEEP_GRAPHICS
    # Boards (the crafted product) stay protected too.
    assert 0x1BD7 in KEEP_GRAPHICS
