"""GoalManager — tracks what the Avatar is trying to accomplish.

Extracted from brain/think.py blackboard keys:
  current_goal, move_target, cached_path, cached_path_target
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


@dataclass
class Goal:
    """A single objective the Avatar is pursuing."""

    place: str = ""
    description: str = ""
    x: int = 0
    y: int = 0
    created_at: float = field(default_factory=time.time)


class GoalManager:
    """Manages Avatar's current goal and movement target."""

    def __init__(self) -> None:
        self.current: Goal | None = None
        self.move_target: tuple[int, int] | None = None
        self._cached_path: list[tuple[int, int]] | None = None
        self._cached_target: tuple[int, int] | None = None
        self._stuck_count: int = 0

    def set_goal(self, place: str, x: int, y: int, description: str = "") -> None:
        """Set a new goal and move target."""
        self.current = Goal(
            place=place,
            description=description or f"Going to {place}",
            x=x, y=y,
        )
        self.move_target = (x, y)
        self.clear_path_cache()
        logger.info("goal_set", place=place, target=f"({x},{y})")

    def arrive(self) -> Goal | None:
        """Mark current goal as arrived. Returns the goal that was completed."""
        goal = self.current
        self.current = None
        self.move_target = None
        self.clear_path_cache()
        self._stuck_count = 0
        if goal:
            logger.info("goal_arrived", place=goal.place)
        return goal

    def abandon(self, reason: str = "") -> Goal | None:
        """Give up on current goal. Returns the abandoned goal."""
        goal = self.current
        self.current = None
        self.move_target = None
        self.clear_path_cache()
        if goal:
            logger.info("goal_abandoned", place=goal.place, reason=reason)
        return goal

    def record_stuck(self) -> int:
        """Record a stuck event. Returns total stuck count."""
        self._stuck_count += 1
        return self._stuck_count

    @property
    def stuck_count(self) -> int:
        return self._stuck_count

    @property
    def has_goal(self) -> bool:
        return self.current is not None

    @property
    def has_move_target(self) -> bool:
        return self.move_target is not None

    # Path cache
    def set_path(self, path: list[tuple[int, int]], target: tuple[int, int]) -> None:
        self._cached_path = path
        self._cached_target = target

    def get_path(self, target: tuple[int, int]) -> list[tuple[int, int]] | None:
        if self._cached_target == target and self._cached_path:
            return list(self._cached_path)
        return None

    def clear_path_cache(self) -> None:
        self._cached_path = None
        self._cached_target = None

    def consume_path_step(self) -> None:
        """Remove first step from cached path."""
        if self._cached_path:
            self._cached_path.pop(0)
            if not self._cached_path:
                self.clear_path_cache()

    # Legacy blackboard bridge
    def to_blackboard(self, bb: dict) -> None:
        """Write goal state to blackboard (legacy compatibility)."""
        if self.current:
            bb["current_goal"] = {
                "place": self.current.place,
                "description": self.current.description,
                "x": self.current.x,
                "y": self.current.y,
            }
        else:
            bb.pop("current_goal", None)

        bb["move_target"] = self.move_target
        bb["cached_path"] = self._cached_path
        bb["cached_path_target"] = self._cached_target

    def from_blackboard(self, bb: dict) -> None:
        """Read goal state from blackboard (legacy compatibility)."""
        goal_data = bb.get("current_goal")
        if goal_data:
            self.current = Goal(
                place=goal_data.get("place", ""),
                description=goal_data.get("description", ""),
                x=goal_data.get("x", 0),
                y=goal_data.get("y", 0),
            )
        else:
            self.current = None

        self.move_target = bb.get("move_target")
        self._cached_path = bb.get("cached_path")
        self._cached_target = bb.get("cached_path_target")
