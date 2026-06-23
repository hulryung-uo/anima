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

    def reset(self) -> None:
        """Drop any RUNNING-resume state this node (and its subtree) holds.

        Default is a no-op: leaf ``Condition``/``Action`` nodes are stateless.
        Composite nodes that remember a suspended child (``Sequence``) override
        this so a parent can *abandon* them cleanly. A reactive priority
        ``Selector`` calls it on any lower-priority child it preempts, so a
        Sequence that was left mid-RUNNING does not silently resume its
        suspended Action the next time control falls back to it.
        """


class Selector(Node):
    """Try children in order until one returns SUCCESS or RUNNING.

    Reactive interruption: this Selector is re-evaluated top-down every tick,
    so a higher-priority branch can preempt a lower-priority one that was left
    RUNNING on a previous tick (e.g. the Survival sequence firing while a
    SkillExec sequence is suspended on a long-RUNNING Action). When that
    happens the preempted child still holds its resume state, and the next time
    the Selector falls back to it the child would blindly resume its suspended
    Action — even though an unbounded amount of world change (a fight, a death
    and resurrect via the higher-priority branch, target loss) happened in
    between, invalidating the preconditions the now-skipped earlier children
    had established. To stay correct the Selector remembers which child was
    RUNNING and ``reset()``s it (and anything after it) whenever an
    earlier-priority child wins instead, forcing a fresh run on fall-back.
    """

    def __init__(self, name: str, children: list[Node]) -> None:
        self.name = name
        self.children = children
        self._running_index: int | None = None

    async def tick(self, ctx: BrainContext) -> Status:
        prev_running = self._running_index
        for index, child in enumerate(self.children):
            result = await child.tick(ctx)
            if result in (Status.SUCCESS, Status.RUNNING):
                # A higher-priority child won than the one suspended last tick:
                # abandon the preempted child (and everything after the winner)
                # so it cannot resume stale state on a later fall-back.
                if prev_running is not None and index < prev_running:
                    for stale in self.children[index + 1:]:
                        stale.reset()
                self._running_index = index if result is Status.RUNNING else None
                return result
        self._running_index = None
        return Status.FAILURE

    def reset(self) -> None:
        self._running_index = None
        for child in self.children:
            child.reset()


class Sequence(Node):
    """Run children in order; all must succeed.

    Stateful RUNNING handling: when a child returns RUNNING the sequence
    remembers that child's index and *resumes* there on the next tick, rather
    than restarting from the first child. Without this, every already-completed
    child (Conditions and side-effecting Actions alike) is re-executed on each
    tick while a later child is still RUNNING — firing packet sends, resource
    spends, etc. more than once. The resume index is cleared on SUCCESS or
    FAILURE so a fresh sequence run always starts from the beginning.

    Guard re-check: leading ``Condition`` children (which are pure, synchronous
    predicates with no side effects) ARE re-evaluated on resume. A guarded
    sequence such as ``[Condition(low_hp), Action(heal)]`` must abandon itself
    the moment its guard goes false — e.g. HP recovers above threshold while
    ``heal`` is still RUNNING on a bandage timer — so the Selector above can
    fall through to other work. Skipping the guard (resuming straight at the
    RUNNING child) would pin the agent to a no-longer-needed action forever.
    Only completed *non-Condition* children are skipped, preserving the
    "don't re-fire side-effecting Actions" property above.
    """

    def __init__(self, name: str, children: list[Node]) -> None:
        self.name = name
        self.children = children
        self._running_index = 0

    async def tick(self, ctx: BrainContext) -> Status:
        start = self._running_index
        # On a resume, re-check any leading Condition guards. They are pure
        # predicates, so re-running is free of side effects; if a guard has
        # since gone false the precondition no longer holds and the sequence
        # must bail rather than blindly resume its RUNNING action.
        if start > 0:
            for index in range(start):
                child = self.children[index]
                if isinstance(child, Condition):
                    if await child.tick(ctx) is not Status.SUCCESS:
                        self._running_index = 0
                        return Status.FAILURE
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

    def reset(self) -> None:
        self._running_index = 0
        for child in self.children:
            child.reset()


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
