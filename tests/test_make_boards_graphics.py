"""make_boards must count BOTH board stack graphics (0x1BD7 and 0x1BDA).

ServUO's Board carries ``[FlipableAttribute(0x1BD7, 0x1BDA)]`` — a board stack
flips its graphic ID just like a log stack flips between 0x1BDD/0x1BE0. The
before/after board delta is the only success signal MakeBoards.execute has, so
counting only 0x1BD7 booked a completed conversion whose result rendered as the
0x1BDA variant as a ``reward=-0.5`` FAILURE — an inverted gathering reward that
made the agent loop a conversion that actually succeeded.
"""
from types import SimpleNamespace

from anima.skills.gathering.make_boards import (
    BOARD_GRAPHIC,
    BOARD_GRAPHICS,
    _count_boards,
)

_BACKPACK = 0x40000015
_BOARD_PRIMARY = 0x1BD7
_BOARD_ALT = 0x1BDA  # the flipped board stack graphic


def _world(*boards: tuple[int, int]):
    """Build a world whose backpack holds the given (graphic, amount) boards."""
    items = {
        0x100 + i: SimpleNamespace(
            container=_BACKPACK, graphic=graphic, amount=amount, hue=0,
        )
        for i, (graphic, amount) in enumerate(boards)
    }
    return SimpleNamespace(items=items)


def test_board_graphics_includes_both_flip_variants():
    assert _BOARD_PRIMARY in BOARD_GRAPHICS
    assert _BOARD_ALT in BOARD_GRAPHICS
    # legacy alias preserved
    assert BOARD_GRAPHIC == _BOARD_PRIMARY


def test_count_boards_counts_alt_flip_graphic():
    # A stack rendered as the 0x1BDA flip variant must be counted — before the
    # fix this returned 0 and a successful conversion looked like a failure.
    assert _count_boards(_world((_BOARD_ALT, 12)), _BACKPACK) == 12


def test_count_boards_counts_primary_graphic_unaffected():
    assert _count_boards(_world((_BOARD_PRIMARY, 7)), _BACKPACK) == 7


def test_count_boards_sums_both_flip_variants():
    world = _world((_BOARD_PRIMARY, 3), (_BOARD_ALT, 5))
    assert _count_boards(world, _BACKPACK) == 8


def test_delta_across_a_flip_is_positive():
    # The conversion produced boards that render as the alt graphic. The
    # before/after delta the skill uses as its success signal must be positive,
    # not zero (which would invert into reward=-0.5).
    before = _count_boards(_world(), _BACKPACK)
    after = _count_boards(_world((_BOARD_ALT, 10)), _BACKPACK)
    assert after - before == 10
    assert after - before > 0


def test_count_boards_no_backpack_is_zero():
    assert _count_boards(_world((_BOARD_ALT, 10)), None) == 0
