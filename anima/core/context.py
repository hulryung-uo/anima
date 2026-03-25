"""AgentContext — typed replacement for the untyped blackboard dict.

Infrastructure items that are set once during construction are typed fields.
Mutable runtime state stays in the legacy blackboard dict until procedures
replace skills (Steps 4-5 of the v2 migration).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from anima.client.connection import UoConnection
from anima.config import Config
from anima.core.bus import EventBus
from anima.core.goals import GoalManager
from anima.map import MapReader
from anima.perception import Perception
from anima.perception.walker import WalkerManager
from anima.persona import Persona

if TYPE_CHECKING:
    from anima.brain.llm import LLMClient
    from anima.memory.database import MemoryDB
    from anima.memory.journal import ActivityJournal
    from anima.monitor.feed import ActivityFeed
    from anima.monitor.metrics import MetricsCollector
    from anima.skills.base import SkillRegistry


@dataclass
class AgentContext:
    """Shared context passed to every behavior tree node, skill, and procedure.

    Replaces the old BrainContext with typed infrastructure fields.
    The blackboard dict remains for mutable runtime state during the migration.
    """

    # --- Core infrastructure (required) ---
    perception: Perception
    conn: UoConnection
    walker: WalkerManager
    cfg: Config

    # --- Optional infrastructure ---
    map_reader: MapReader | None = None
    llm: LLMClient | None = None
    memory_db: MemoryDB | None = None

    # --- Typed fields (previously in blackboard) ---
    persona: Persona | None = None
    bus: EventBus | None = None
    feed: ActivityFeed | None = None
    goals: GoalManager | None = None
    skill_registry: SkillRegistry | None = None
    journal: ActivityJournal | None = None
    forum_client: Any = None
    metrics: MetricsCollector | None = None

    # --- Legacy blackboard (mutable runtime state) ---
    blackboard: dict[str, Any] = field(default_factory=dict)
