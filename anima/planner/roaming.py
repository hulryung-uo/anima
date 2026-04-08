"""Location roaming — move-to-location routing and mine rotation.

Extracted from planner.py in Task 4.3 to shrink the main planner file
and keep location-picking logic in one focused module.

The RoamingHelper class takes a Planner reference so it can read/write
`self._planner._failed_destinations` and other planner-private state.
"""

from __future__ import annotations

import re
import time as _time
from typing import TYPE_CHECKING

import structlog

from anima.planner.helpers import _MoveToProcedure

if TYPE_CHECKING:
    from anima.core.context import AgentContext
    from anima.planner.planner import Planner

logger = structlog.get_logger()


def _find_waypoint_toward(sx, sy, tx, ty, locations) -> object | None:
    """Find the best intermediate waypoint between (sx,sy) and (tx,ty).

    Picks a waypoint that:
    1. Is closer to the target than we are
    2. Is closer to us than the target is
    3. Is roughly on the path (not a big detour)
    """
    current_dist = max(abs(tx - sx), abs(ty - sy))
    best = None
    best_score = float("inf")

    for loc in locations:
        loc_to_target = max(abs(tx - loc.x), abs(ty - loc.y))
        loc_to_us = max(abs(sx - loc.x), abs(sy - loc.y))

        # Must be closer to target than we are
        if loc_to_target >= current_dist:
            continue
        # Must be reachable (not too far from us)
        if loc_to_us >= current_dist:
            continue
        # Must be closer to us than the target
        if loc_to_us < 5:
            continue  # already here

        # Score: lower is better — prefer waypoints that progress toward target
        score = loc_to_us + loc_to_target
        if score < best_score:
            best_score = score
            best = loc

    return best


class RoamingHelper:
    """Handles "move to named location" and mine rotation for the planner."""

    def __init__(self, planner: "Planner") -> None:
        self._planner = planner

    def is_destination_failed(self, x: int, y: int) -> bool:
        """Check if a destination recently failed to be reached (5-min cooldown)."""
        import time
        ts = self._planner._failed_destinations.get((x, y))
        if ts is None:
            return False
        if time.time() - ts < 300.0:
            return True
        del self._planner._failed_destinations[(x, y)]
        return False

    async def move_to_location(
        self, ctx: "AgentContext", *keywords: str, max_dist: int = 300,
    ):
        """Find nearest location matching any keyword and move there.

        max_dist caps the search radius to avoid cross-city navigation attempts
        (e.g., trying Britain vendors when in Minoc).
        """
        from anima.world_knowledge import ALL_LOCATIONS

        ss = ctx.perception.self_state

        # Mark locations we're already standing at as temporarily failed.
        # This prevents ping-pong: arrive at vendor location → can't sell →
        # walk to next vendor → can't sell → walk back to first one → repeat.
        for loc in ALL_LOCATIONS:
            name_lower = loc.name.lower()
            if any(kw in name_lower for kw in keywords):
                dist = max(abs(loc.x - ss.x), abs(loc.y - ss.y))
                if dist <= 3 and (loc.x, loc.y) not in self._planner._failed_destinations:
                    self._planner._failed_destinations[(loc.x, loc.y)] = _time.time()

        best = None
        best_dist = 999999
        for loc in ALL_LOCATIONS:
            name_lower = loc.name.lower()
            if any(kw in name_lower for kw in keywords):
                dist = max(abs(loc.x - ss.x), abs(loc.y - ss.y))
                if dist > max_dist:
                    continue  # skip locations in other cities
                if dist > 3 and dist < best_dist:
                    if self.is_destination_failed(loc.x, loc.y):
                        continue
                    best_dist = dist
                    best = loc

        if best:
            logger.info("planner_move_to", target=best.name, dist=best_dist)
            if ctx.bus:
                ctx.bus.publish("movement.start", {
                    "message": f"→ Moving to {best.name} (dist {best_dist})",
                    "importance": 2,
                })
            return _MoveToProcedure(best.name, best.x, best.y)
        return None

    def mark_nearby_mine_exhausted(self, ctx: "AgentContext", ss) -> None:
        """Mark the mine LOCATION nearest to the player as exhausted.

        Called when _find_mineable_tile returns None — meaning every ore
        bank within MOVE_RADIUS is depleted. The exhausted-location flag
        causes try_move_to_activity to prefer a different mine for ~5 min.
        """
        from anima.world_knowledge import ALL_LOCATIONS
        _MINE_RE = re.compile(r'\b(mine|mining)\b', re.IGNORECASE)
        nearest = None
        nearest_dist = 999
        for loc in ALL_LOCATIONS:
            if not _MINE_RE.search(loc.name):
                continue
            d = max(abs(loc.x - ss.x), abs(loc.y - ss.y))
            if d < nearest_dist:
                nearest_dist = d
                nearest = loc
        if nearest is None or nearest_dist > 30:
            return  # not standing near a known mine
        exhausted = ctx.blackboard.setdefault("exhausted_mines", {})
        exhausted[nearest.name] = _time.time()
        logger.info(
            "planner_mine_exhausted_marked",
            mine=nearest.name, dist=nearest_dist,
        )

    async def try_move_to_activity(self, ctx: "AgentContext"):
        """If no procedure can start, walk toward primary activity location.

        Uses waypoint routing: if the target is far, finds intermediate
        waypoints along the way to avoid getting stuck on building walls.

        Skips mine locations marked as exhausted within the last 5 min so
        the agent rotates between mining areas instead of camping a single
        depleted spot.
        """
        from anima.world_knowledge import ALL_LOCATIONS

        ss = ctx.perception.self_state

        # Find nearest activity location using word boundaries so that
        # "mine" matches "East Mine" but not "Miners Guild".
        _ACTIVITY_RE = re.compile(r'\b(mine|mining|mountain|forest)\b', re.IGNORECASE)
        max_activity_dist = 300  # prevent cross-city routing
        EXHAUSTED_TTL = 300.0  # 5 min
        exhausted = ctx.blackboard.get("exhausted_mines", {})
        now = _time.time()
        # Drop stale entries
        for k in [k for k, ts in exhausted.items() if now - ts > EXHAUSTED_TTL]:
            del exhausted[k]
        mine_loc = None
        best_dist = 999999
        for loc in ALL_LOCATIONS:
            if _ACTIVITY_RE.search(loc.name):
                if self.is_destination_failed(loc.x, loc.y):
                    continue
                if loc.name in exhausted:
                    continue  # recently exhausted — pick a different mine
                dist = max(abs(loc.x - ss.x), abs(loc.y - ss.y))
                if dist <= 5:
                    continue  # Already here — skip to find next location
                if dist > max_activity_dist:
                    continue  # Skip locations in other cities
                if dist < best_dist:
                    best_dist = dist
                    mine_loc = loc

        if not mine_loc:
            return None

        # If far away, find intermediate waypoints
        # Pick the waypoint closest to the line between current pos and target
        target = mine_loc
        if best_dist > 30:
            waypoint = _find_waypoint_toward(ss.x, ss.y, target.x, target.y, ALL_LOCATIONS)
            if waypoint and not self.is_destination_failed(waypoint.x, waypoint.y):
                logger.info(
                    "planner_waypoint_routing",
                    via=waypoint.name,
                    pos=f"({waypoint.x},{waypoint.y})",
                    final_target=target.name,
                )
                return _MoveToProcedure(waypoint.name, waypoint.x, waypoint.y)

        logger.info(
            "planner_moving_to_activity",
            target=target.name,
            pos=f"({target.x},{target.y})",
            dist=best_dist,
        )
        if ctx.bus:
            ctx.bus.publish("movement.start", {
                "message": f"⛏ Heading to {target.name} (dist {best_dist})",
                "importance": 2,
            })
        return _MoveToProcedure(target.name, target.x, target.y)
