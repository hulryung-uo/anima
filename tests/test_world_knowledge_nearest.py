"""Tests for nearest-location distance using the navigable point."""

from unittest.mock import patch

from anima.world_knowledge import (
    Location,
    find_location,
    format_locations_for_llm,
    nearest_locations,
)


def test_nearest_uses_approach_point_for_distance():
    """An indoor location's distance is measured to its approach point.

    ``indoor`` sits far away by its raw (x, y) but has an approach point right
    next to the agent; ``outdoor`` is moderately far. The agent actually walks
    to the approach point, so ``indoor`` must rank as nearest.
    """
    indoor = Location(
        "Deep Indoor Shop",
        x=5000,
        y=5000,
        description="inside a building",
        approach_x=105,
        approach_y=105,
    )
    outdoor = Location("Outdoor Plaza", x=200, y=200, description="open square")

    with patch("anima.world_knowledge.ALL_LOCATIONS", [outdoor, indoor]):
        results = nearest_locations(100, 100, count=2)

    # Closest first: indoor (approach 105,105 -> dist 5) beats outdoor (dist 100).
    assert results[0][0].name == "Deep Indoor Shop"
    assert results[0][1] == 5
    assert results[1][0].name == "Outdoor Plaza"
    assert results[1][1] == 100


def test_outdoor_location_distance_unchanged():
    """With no approach point, distance is the raw Chebyshev distance."""
    loc = Location("Plain Spot", x=130, y=140, description="")
    with patch("anima.world_knowledge.ALL_LOCATIONS", [loc]):
        results = nearest_locations(100, 100, count=1)
    # Chebyshev: max(|130-100|, |140-100|) = 40.
    assert results[0][1] == 40


def test_llm_format_reports_travel_distance_for_indoor():
    """The LLM summary reports the real travel distance, not the indoor one."""
    indoor = Location(
        "Hidden Vault",
        x=9000,
        y=9000,
        description="deep inside",
        approach_x=110,
        approach_y=100,
    )
    with patch("anima.world_knowledge.ALL_LOCATIONS", [indoor]):
        text = format_locations_for_llm(100, 100, count=1)
    # Travel distance is max(|110-100|, |100-100|) = 10, not the ~8900 raw gap.
    assert "~10 steps away" in text
    assert "8900" not in text
    assert "9000" not in text.split("steps away")[0].split("~")[1]


def test_count_limits_results():
    locs = [Location(f"L{i}", x=100 + i, y=100, description="") for i in range(10)]
    with patch("anima.world_knowledge.ALL_LOCATIONS", locs):
        results = nearest_locations(100, 100, count=3)
    assert len(results) == 3
    # Strictly non-decreasing distances (sorted).
    dists = [d for _, d in results]
    assert dists == sorted(dists)


def test_minoc_tanner_uses_reachable_exterior_approach_point():
    """Observed live loop: walking to raw Minoc Tanner coords stops at the exterior.

    Grimm repeatedly failed with ``Could not reach Walk to Minoc Tanner`` while
    ending at (2517,518), seven tiles from the raw shop coordinate (2524,524).
    Treat that exterior tile as the navigable point so waypoint routing does
    not path into the blocked interior/tight shop coordinate again.
    """
    tanner = find_location("Minoc Tanner")

    assert tanner is not None
    assert (tanner.x, tanner.y) == (2524, 524)
    assert (tanner.nav_x, tanner.nav_y) == (2517, 518)
