"""Procedures — async game workflows that replace Skills.

Each procedure is a self-contained async workflow:
- Checks preconditions (can_start)
- Executes a sequence of action primitives (execute)
- Returns a structured result with failure reason
- Base class auto-logs every result to ActionLog (SQLite)
"""

from anima.procedures.base import (
    FailureReason,
    Procedure,
    ProcedureRegistry,
    ProcedureResult,
)

__all__ = ["FailureReason", "Procedure", "ProcedureRegistry", "ProcedureResult"]
