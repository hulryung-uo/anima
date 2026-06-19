"""Behavior tree framework for the Anima brain."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import Enum, auto

from anima.core.context import AgentContext

# Backward-compat alias — all existing code that imports BrainContext keeps working.
BrainContext = AgentContext


class Status(Enum):
    SUCCESS = auto()
    FAILURE = auto()
    RUNNING = auto()


class Node(ABC):
    """Abstract base class for behavior tree nodes."""

    @abstractmethod
    async def tick(self, ctx: BrainContext) -> Status: ...


class Selector(Node):
    """Try children in order until one returns SUCCESS or RUNNING."""

    def __init__(self, name: str, children: list[Node]) -> None:
        self.name = name
        self.children = children

    async def tick(self, ctx: BrainContext) -> Status:
        for child in self.children:
            result = await child.tick(ctx)
            if result in (Status.SUCCESS, Status.RUNNING):
                return result
        return Status.FAILURE


class Sequence(Node):
    """Run children in order; all must succeed.

    Stateful RUNNING handling: when a child returns RUNNING the sequence
    remembers that child's index and *resumes* there on the next tick, rather
    than restarting from the first child. Without this, every already-completed
    child (Conditions and side-effecting Actions alike) is re-executed on each
    tick while a later child is still RUNNING — firing packet sends, resource
    spends, etc. more than once. The resume index is cleared on SUCCESS or
    FAILURE so a fresh sequence run always starts from the beginning.
    """

    def __init__(self, name: str, children: list[Node]) -> None:
        self.name = name
        self.children = children
        self._running_index = 0

    async def tick(self, ctx: BrainContext) -> Status:
        start = self._running_index
        for index in range(start, len(self.children)):
            child = self.children[index]
            result = await child.tick(ctx)
            if result is Status.RUNNING:
                # Suspend here; resume at this child next tick.
                self._running_index = index
                return result
            if result is Status.FAILURE:
                self._running_index = 0
                return result
        self._running_index = 0
        return Status.SUCCESS


class Condition(Node):
    """Synchronous predicate check — returns SUCCESS if true, FAILURE otherwise."""

    def __init__(self, name: str, predicate: Callable[[BrainContext], bool]) -> None:
        self.name = name
        self.predicate = predicate

    async def tick(self, ctx: BrainContext) -> Status:
        return Status.SUCCESS if self.predicate(ctx) else Status.FAILURE


class Action(Node):
    """Async action — wraps an async callable that returns Status."""

    def __init__(
        self,
        name: str,
        func: Callable[[BrainContext], Awaitable[Status]],
    ) -> None:
        self.name = name
        self.func = func

    async def tick(self, ctx: BrainContext) -> Status:
        return await self.func(ctx)
