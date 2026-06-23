"""CommandBus — queue for external steering commands.

WebSocket server pushes commands in; Planner pops them out each tick.
Thread-safe via asyncio.Queue (single event loop).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Command:
    cmd: str
    params: dict[str, Any] = field(default_factory=dict)


class CommandBus:
    """External command queue for agent steering."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Command] = asyncio.Queue()
        self._paused = False
        self._override_procedure: str | None = None
        self._override_go_to: tuple[int, int] | None = None
        self._goal: str | None = None
        # ``None`` is both a valid update (clear the goal) and the steady-state
        # value for "no goal", so track whether a fresh set/clear command has
        # arrived separately from the value itself.
        self._goal_update_pending = False

    # --- State properties (read by Planner) ---

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, v: bool) -> None:
        self._paused = v

    @property
    def override_procedure(self) -> str | None:
        """Next procedure to force-run. Consumed once by planner."""
        val = self._override_procedure
        self._override_procedure = None
        return val

    @property
    def override_go_to(self) -> tuple[int, int] | None:
        """Force move target. Consumed once by planner."""
        val = self._override_go_to
        self._override_go_to = None
        return val

    @property
    def goal(self) -> str | None:
        return self._goal

    def set_goal(self, goal: str | None) -> None:
        self._goal = goal
        self._goal_update_pending = True

    def consume_goal_update(self) -> tuple[bool, str | None]:
        """Return and clear the latest companion goal update.

        The boolean says whether an update was present; the value is the new
        goal text, or ``None`` when the update is an explicit clear. This lets
        the planner distinguish "no command this tick" from "clear the current
        companion goal".
        """
        if not self._goal_update_pending:
            return False, None
        self._goal_update_pending = False
        return True, self._goal

    # --- Push/Pop ---

    def push(self, cmd: str, **params: Any) -> None:
        self._queue.put_nowait(Command(cmd=cmd, params=params))

    def pop(self) -> Command | None:
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def drain(self) -> list[Command]:
        """Pop all pending commands."""
        cmds = []
        while True:
            c = self.pop()
            if c is None:
                break
            cmds.append(c)
        return cmds
