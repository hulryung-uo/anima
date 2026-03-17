"""Behavior tree framework for the Anima brain."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from anima.brain.llm import LLMClient
from anima.client.connection import UoConnection
from anima.config import Config
from anima.map import MapReader
from anima.memory.database import MemoryDB
from anima.perception import Perception
from anima.perception.walker import WalkerManager


class Status(Enum):
    SUCCESS = auto()
    FAILURE = auto()
    RUNNING = auto()


@dataclass
class BrainContext:
    """Shared context passed to every behavior tree node."""

    perception: Perception
    conn: UoConnection
    walker: WalkerManager
    map_reader: MapReader | None
    cfg: Config
    llm: LLMClient | None = None
    memory_db: MemoryDB | None = None
    blackboard: dict[str, Any] = field(default_factory=dict)


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
    """Run children in order; all must succeed."""

    def __init__(self, name: str, children: list[Node]) -> None:
        self.name = name
        self.children = children

    async def tick(self, ctx: BrainContext) -> Status:
        for child in self.children:
            result = await child.tick(ctx)
            if result in (Status.FAILURE, Status.RUNNING):
                return result
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
