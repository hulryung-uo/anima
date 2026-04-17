"""Location roaming — move-to-location routing and mine rotation.

Extracted from planner.py in Task 4.3 to shrink the main planner file
and keep location-picking logic in one focused module.

The RoamingHelper class takes a Planner reference so it can read/write
`self._planner._failed_destinations` and other planner-private state.
"""

from __future__ import annotations

import re
import time as _time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from anima.planner.helpers import _MoveToProcedure

if TYPE_CHECKING:
    from anima.core.context import AgentContext
    from anima.planner.planner import Planner
    from anima.world_knowledge import Location

logger = structlog.get_logger()

_FAILURE_DECAY = 300.0  # 5 minutes — failed_attempts reset to 0 after this


@dataclass
class LocationScore:
    location: "Location"
    distance: int
    failed_attempts: int = 0
    success_rate: float = 1.0
    last_visit: float = 0.0

    def score(self) -> float:
        """Higher is better. Base 1000 minus distance, adjusted by history."""
        base = 1000.0 - self.distance
        penalty = self.failed_attempts * 50.0
        bonus = self.success_rate * 100.0
        # Freshness bonus: prefer locations we haven't visited recently
        # (caps at 10 points after 10 minutes)
        import time as _t
        age = _t.time() - self.last_visit if self.last_visit else 600
        freshness = min(age / 60.0, 10.0)
        return base - penalty + bonus + freshness


class LocationStats:
    """Per-location visit history for cost-weighted selection."""

    def __init__(self) -> None:
        self._visits: dict[str, int] = {}
        self._successes: dict[str, int] = {}
        self._last_visit: dict[str, float] = {}
        self._failed_attempts: dict[str, int] = {}
        self._failed_at: dict[str, float] = {}  # ts of last failure

    def record_visit(self, name: str, success: bool) -> None:
        """Record a visit outcome for a named location."""
        self._visits[name] = self._visits.get(name, 0) + 1
        if success:
            self._successes[name] = self._successes.get(name, 0) + 1
        self._last_visit[name] = _time.time()

    def record_failure(self, name: str) -> None:
        """Record a navigation failure for a named location."""
        self._failed_attempts[name] = self._failed_attempts.get(name, 0) + 1
        self._failed_at[name] = _time.time()

    def get_score(self, location: "Location", distance: int) -> LocationScore:
        """Return a LocationScore for the given location and distance."""
        name = location.name

        # Decay failed_attempts if last failure was more than 5 minutes ago
        failed_at = self._failed_at.get(name)
        if failed_at is not None and _time.time() - failed_at > _FAILURE_DECAY:
            failed = 0
        else:
            failed = self._failed_attempts.get(name, 0)

        visits = self._visits.get(name, 0)
        successes = self._successes.get(name, 0)
        success_rate = (successes / visits) if visits > 0 else 1.0

        last_visit = self._last_visit.get(name, 0.0)

        return LocationScore(
            location=location,
            distance=distance,
            failed_attempts=failed,
            success_rate=success_rate,
            last_visit=last_visit,
        )


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
        self._location_stats = LocationStats()

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

        # Respect the 30s move-failure cooldown. Previously only some call
        # sites checked _move_fail_until; ungated priorities (e.g. Priority 4c
        # buy-tools at planner.py:751,773) looped on failed pathfinding every
        # ~5s, preventing _idle_ticks from growing and blocking the deadlock
        # resolver from ever firing Strategy 5 (which is what clears the
        # cooldown intentionally). Centralising the gate here makes all paths
        # idle during the cooldown so the designed escalation can run.
        if _time.time() < self._planner._move_fail_until:
            return None

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

        candidates: list[LocationScore] = []
        for loc in ALL_LOCATIONS:
            name_lower = loc.name.lower()
            if any(kw in name_lower for kw in keywords):
                dist = max(abs(loc.x - ss.x), abs(loc.y - ss.y))
                if dist > max_dist:
                    continue  # skip locations in other cities
                if dist <= 3:
                    continue  # already here (handled above as temporarily failed)
                if self.is_destination_failed(loc.x, loc.y):
                    continue
                candidates.append(self._location_stats.get_score(loc, dist))

        if not candidates:
            return None

        best_scored = max(candidates, key=lambda s: s.score())
        best = best_scored.location
        best_dist = best_scored.distance

        logger.info("planner_move_to", target=best.name, dist=best_dist)

        # Waypoint routing for longer distances — urban areas have building
        # walls that exhaust the A* budget on direct point-to-point paths.
        if best_dist > 20:
            from anima.world_knowledge import ALL_LOCATIONS as _all_locs
            waypoint = _find_waypoint_toward(ss.x, ss.y, best.x, best.y, _all_locs)
            if waypoint and not self.is_destination_failed(waypoint.x, waypoint.y):
                logger.info(
                    "planner_waypoint_routing",
                    via=waypoint.name,
                    pos=f"({waypoint.nav_x},{waypoint.nav_y})",
                    final_target=best.name,
                )
                return _MoveToProcedure(waypoint.name, waypoint.nav_x, waypoint.nav_y)

        if ctx.bus:
            ctx.bus.publish("movement.start", {
                "message": f"→ Moving to {best.name} (dist {best_dist})",
                "importance": 2,
            })
        return _MoveToProcedure(best.name, best.nav_x, best.nav_y)

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
        candidates: list[LocationScore] = []
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
                candidates.append(self._location_stats.get_score(loc, dist))

        if not candidates:
            return None

        best_scored = max(candidates, key=lambda s: s.score())
        mine_loc = best_scored.location
        best_dist = best_scored.distance

        # If far away, find intermediate waypoints
        # Pick the waypoint closest to the line between current pos and target
        target = mine_loc
        if best_dist > 30:
            waypoint = _find_waypoint_toward(ss.x, ss.y, target.x, target.y, ALL_LOCATIONS)
            if waypoint and not self.is_destination_failed(waypoint.x, waypoint.y):
                logger.info(
                    "planner_waypoint_routing",
                    via=waypoint.name,
                    pos=f"({waypoint.nav_x},{waypoint.nav_y})",
                    final_target=target.name,
                )
                return _MoveToProcedure(waypoint.name, waypoint.nav_x, waypoint.nav_y)

        logger.info(
            "planner_moving_to_activity",
            target=target.name,
            pos=f"({target.nav_x},{target.nav_y})",
            dist=best_dist,
        )
        if ctx.bus:
            ctx.bus.publish("movement.start", {
                "message": f"⛏ Heading to {target.name} (dist {best_dist})",
                "importance": 2,
            })
        return _MoveToProcedure(target.name, target.nav_x, target.nav_y)
