"""Regression: waypoint routing scores the navigable point, not raw coords.

``_find_waypoint_toward`` returns a waypoint that the caller then walks to via
``_MoveToProcedure(waypoint.name, waypoint.nav_x, waypoint.nav_y)``. For an
indoor location the raw ``.x``/``.y`` is a point *inside* a building while the
approach point (``approach_x``/``approach_y`` -> ``nav_x``/``nav_y``) can be far
away. Selecting on the raw coords could pick an indoor waypoint that looks like
clean on-path progress while its approach point is actually a detour backward —
the agent would lose ground. The helper must measure the navigable point.
"""
from anima.planner.roaming import _find_waypoint_toward
from anima.world_knowledge import Location


def test_waypoint_scored_by_nav_point_not_raw_coords():
    # Agent at (0,0) heading to a target 100 tiles away on the diagonal.
    sx, sy, tx, ty = 0, 0, 100, 100

    # A decoy indoor waypoint: its RAW coords (50,50) sit perfectly on the path
    # midpoint, but its outdoor approach point is at (0,-80) — a big detour
    # *behind* the agent (further from the target than we already are).
    decoy = Location(
        "Indoor Decoy", x=50, y=50, approach_x=0, approach_y=-80,
    )
    # A genuine outdoor waypoint slightly off the ideal line but a real advance:
    # its nav point (40,45) is closer to both us and the target.
    good = Location("Outdoor Good", x=40, y=45)

    chosen = _find_waypoint_toward(sx, sy, tx, ty, [decoy, good])

    # The decoy's nav point (0,-80) is 180 tiles from the target — further than
    # our current 100 — so it must be rejected; the genuine waypoint wins.
    assert chosen is good


def test_indoor_decoy_alone_is_rejected():
    """With only the decoy available, no waypoint should be returned.

    Under the pre-fix raw-coord logic the decoy (raw 50,50) passed every gate
    and was returned, sending the agent to its approach point (0,-80) — a move
    directly away from the target. Scoring the nav point rejects it outright.
    """
    sx, sy, tx, ty = 0, 0, 100, 100
    decoy = Location(
        "Indoor Decoy", x=50, y=50, approach_x=0, approach_y=-80,
    )
    assert _find_waypoint_toward(sx, sy, tx, ty, [decoy]) is None


def test_outdoor_waypoint_still_selected():
    """Sanity: a plain outdoor location on the path is still chosen (no
    approach point means nav_x/nav_y == x/y, so behaviour is unchanged)."""
    sx, sy, tx, ty = 0, 0, 100, 0
    midpoint = Location("Road Marker", x=50, y=0)
    chosen = _find_waypoint_toward(sx, sy, tx, ty, [midpoint])
    assert chosen is midpoint
