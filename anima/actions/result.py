"""ActionResult — return type for all action primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActionResult:
    """Result of an action primitive."""

    success: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
