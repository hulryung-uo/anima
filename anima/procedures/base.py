"""Procedure base class — async game workflows with ActionLog recording."""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from anima.core.context import AgentContext
    from anima.memory.database import MemoryDB

logger = structlog.get_logger()


class FailureReason(Enum):
    """Why a procedure failed — used by the planner to decide what to do next."""

    BLOCKED = "blocked"                     # can't reach location / path blocked
    INTERRUPTED = "interrupted"             # attacked, HP low
    MISSING_RESOURCE = "missing_resource"   # no tools, no materials
    WRONG_LOCATION = "wrong_location"       # not near forge/anvil/bank
    PERMANENT = "permanent"                 # skill too low, impossible


@dataclass
class ProcedureResult:
    """Result of executing a procedure."""

    success: bool
    reason: FailureReason | None = None
    message: str = ""
    items_gained: list[int] = field(default_factory=list)
    gold_changed: int = 0
    skill_gains: dict[int, float] = field(default_factory=dict)
    next_suggestion: str | None = None  # continuation hint for planner
    details: dict[str, Any] = field(default_factory=dict)


class Procedure(ABC):
    """Abstract base class for game procedures.

    Subclasses implement can_start() and execute(). The base class
    wraps execute() with timing and auto-logs every result to ActionLog.
    """

    name: str = ""
    description: str = ""

    @abstractmethod
    async def can_start(self, ctx: AgentContext) -> bool:
        """Check if preconditions are met to run this procedure."""
        ...

    @abstractmethod
    async def execute(self, ctx: AgentContext) -> ProcedureResult:
        """Run the procedure. Linear async code with check-between-steps."""
        ...

    async def diagnose(self, ctx: AgentContext) -> str | None:
        """Return None if can_start, or a short reason why not."""
        if await self.can_start(ctx):
            return None
        return f"{self.name}: preconditions not met"

    async def run(self, ctx: AgentContext) -> ProcedureResult:
        """Execute with timing and ActionLog recording. Call this, not execute()."""
        ss = ctx.perception.self_state
        start = time.monotonic()
        location = (ss.x, ss.y)

        try:
            result = await self.execute(ctx)
        except Exception as e:
            logger.error("procedure_error", procedure=self.name, error=str(e))
            result = ProcedureResult(
                success=False,
                reason=FailureReason.PERMANENT,
                message=f"Exception: {e}",
            )

        duration_ms = (time.monotonic() - start) * 1000

        # Auto-log to ActionLog
        await _record_action_log(
            memory_db=ctx.memory_db,
            agent_name=ctx.persona.name if ctx.persona else "unknown",
            procedure=self.name,
            location_x=location[0],
            location_y=location[1],
            result_str="success" if result.success else (result.reason.value if result.reason else "failure"),
            message=result.message,
            duration_ms=duration_ms,
            details=result.details,
        )

        logger.info(
            "procedure_result",
            procedure=self.name,
            success=result.success,
            reason=result.reason.value if result.reason else None,
            duration_ms=f"{duration_ms:.0f}",
            message=result.message,
        )
        return result

    def __repr__(self) -> str:
        return f"<Procedure {self.name}>"


class ProcedureRegistry:
    """Registry of all available procedures."""

    def __init__(self) -> None:
        self._procedures: dict[str, Procedure] = {}

    def register(self, procedure: Procedure) -> None:
        self._procedures[procedure.name] = procedure

    def get(self, name: str) -> Procedure | None:
        return self._procedures.get(name)

    @property
    def all_procedures(self) -> list[Procedure]:
        return list(self._procedures.values())

    async def available(self, ctx: AgentContext) -> list[Procedure]:
        """Return procedures whose preconditions are currently met."""
        result = []
        for proc in self._procedures.values():
            if await proc.can_start(ctx):
                result.append(proc)
        return result

    def describe_all(self) -> str:
        """Build a description of all procedures for LLM context."""
        lines = []
        for proc in self._procedures.values():
            lines.append(f"- {proc.name}: {proc.description}")
        return "\n".join(lines)


async def _record_action_log(
    memory_db: MemoryDB | None,
    agent_name: str,
    procedure: str,
    location_x: int,
    location_y: int,
    result_str: str,
    message: str,
    duration_ms: float,
    details: dict[str, Any] | None = None,
) -> None:
    """Write an ActionLog entry to SQLite. Fails silently with warning."""
    if memory_db is None:
        return
    try:
        now = time.time()
        details_json = json.dumps(details or {})
        await memory_db.db.execute(
            """INSERT INTO action_logs
               (timestamp, agent, procedure, location_x, location_y,
                result, message, duration_ms, details)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, agent_name, procedure, location_x, location_y,
             result_str, message, duration_ms, details_json),
        )
        await memory_db.db.commit()
    except Exception as e:
        logger.warning("action_log_write_failed", error=str(e), procedure=procedure)
