"""Avatar — the central entity representing one character in the game world.

Avatar owns all state, connections, and the EventBus.
Brain observes Avatar and issues commands.
Subscribers observe Avatar through the EventBus.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from anima.client.connection import UoConnection
from anima.client.handler import PacketHandler
from anima.config import Config
from anima.core.bus import EventBus
from anima.core.subscriber import LogSubscriber, MetricsSubscriber
from anima.map import MapReader
from anima.perception import Perception
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager
from anima.persona import Persona, load_persona_by_name

if TYPE_CHECKING:
    from anima.brain.llm import LLMClient
    from anima.memory.database import MemoryDB
    from anima.monitor.feed import ActivityFeed
    from anima.skills.base import SkillRegistry

logger = structlog.get_logger()


class Avatar:
    """One character in the game world — state + bus + connection."""

    def __init__(
        self,
        cfg: Config,
        conn: UoConnection,
        perception: Perception,
        walker: WalkerManager,
        pkt_handler: PacketHandler,
        persona: Persona,
        bus: EventBus,
        map_reader: MapReader | None = None,
        llm: LLMClient | None = None,
        memory_db: MemoryDB | None = None,
        feed: ActivityFeed | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self.cfg = cfg
        self.conn = conn
        self.perception = perception
        self.walker = walker
        self.pkt_handler = pkt_handler
        self.persona = persona
        self.bus = bus
        self.map_reader = map_reader
        self.llm = llm
        self.memory_db = memory_db
        self.feed = feed
        self.skill_registry = skill_registry

        # Subscribers (kept for cleanup)
        self._subscribers: list = []

    @property
    def name(self) -> str:
        return self.persona.name

    @property
    def self_state(self):
        return self.perception.self_state

    @staticmethod
    async def create(
        cfg: Config,
        delete_existing: bool = False,
    ) -> Avatar:
        """Create and initialize a fully connected Avatar."""

        # 1. Connection + Perception
        conn = UoConnection(timeout=cfg.client.connection_timeout)
        perception = Perception(player_serial=0)
        walker = WalkerManager(perception.self_state, perception.events)
        pkt_handler = PacketHandler()
        register_handlers(pkt_handler, perception, walker)

        # 2. Persona
        persona = load_persona_by_name(cfg.character.persona)
        char_name = cfg.character.name
        if char_name == "Anima" and persona.name != "Anima":
            char_name = persona.name

        # 3. Login
        result = await conn.login(
            cfg.server.host,
            cfg.server.port,
            cfg.account.username,
            cfg.account.password,
            character_name=char_name,
            character_template=cfg.character.template,
            character_persona=cfg.character.persona,
            character_city=cfg.character.city_index,
            delete_existing=delete_existing,
            packet_handler=pkt_handler,
            perception=perception,
        )
        perception.self_state.serial = result.serial

        # 4. EventBus
        bus = EventBus()
        perception.events.connect_bus(bus)

        log_sub = LogSubscriber("data/events.jsonl")
        for pattern in log_sub.topics():
            bus.subscribe(pattern, log_sub.on_event)

        metrics_sub = MetricsSubscriber()
        for pattern in metrics_sub.topics():
            bus.subscribe(pattern, metrics_sub.on_event)

        logger.info("event_bus_ready", subscribers=bus.subscriber_count)

        # 5. Map reader
        resource_dir = Path(cfg.map.resource_dir).expanduser()
        map_reader: MapReader | None = None
        if resource_dir.exists():
            map_reader = MapReader(resource_dir)
            logger.info("map_reader_loaded", resource_dir=str(resource_dir))

        # 6. LLM
        from anima.brain.llm import LLMClient

        llm = LLMClient(
            provider=cfg.llm.provider,
            model=cfg.llm.model,
            base_url=cfg.llm.base_url,
            api_key=cfg.llm.api_key,
            temperature=cfg.llm.temperature,
            timeout=cfg.llm.timeout,
        )
        logger.info("llm_client_ready", provider=cfg.llm.provider, model=cfg.llm.model)

        # 7. Memory
        from anima.memory.database import MemoryDB

        memory_db = MemoryDB(cfg.memory.db_path)
        await memory_db.init()

        # 8. Feed + legacy metrics
        from anima.monitor.feed import ActivityFeed
        from anima.monitor.metrics import MetricsCollector

        feed = ActivityFeed(max_events=cfg.monitor.max_events)
        metrics_collector = MetricsCollector()

        def _bus_to_metrics(topic: str, data: dict) -> None:
            if topic == "avatar.walk_confirmed":
                metrics_collector.record("walk_confirmed", data)
            elif topic == "avatar.walk_denied":
                metrics_collector.record("walk_denied", data)
            elif topic == "avatar.skill_change" and "diff" in data:
                metrics_collector.record("skill_gain", data)

        bus.subscribe("avatar.*", _bus_to_metrics)

        # 9. Skills
        from anima.skills.base import SkillRegistry
        from anima.skills.combat.healing import HealSelf
        from anima.skills.combat.melee import MeleeAttack
        from anima.skills.crafting.carpentry import CraftCarpentry
        from anima.skills.crafting.smelt import SmeltOre
        from anima.skills.crafting.tinker import CraftTinker
        from anima.skills.gathering.lumber import ChopWood
        from anima.skills.gathering.make_boards import MakeBoards
        from anima.skills.gathering.mine import MineOre
        from anima.skills.trade.vendor import BuyFromNpc, SellToNpc

        skill_registry = SkillRegistry()
        for skill_cls in [
            HealSelf, MeleeAttack, MineOre, ChopWood, MakeBoards,
            SmeltOre, CraftTinker, CraftCarpentry, BuyFromNpc, SellToNpc,
        ]:
            skill_registry.register(skill_cls())
        logger.info("skills_registered", count=len(skill_registry.all_skills))

        # 10. Forum
        forum_client = None
        if cfg.forum.enabled and cfg.forum.api_key:
            from anima.skills.tavern_client import TavernForumClient
            forum_client = TavernForumClient(cfg.forum.base_url, cfg.forum.api_key)
            logger.info("forum_client_ready", base_url=cfg.forum.base_url)

        logger.info(
            "avatar_ready",
            name=persona.name,
            serial=f"0x{result.serial:08X}",
            position=f"({result.x}, {result.y}, {result.z})",
        )

        avatar = Avatar(
            cfg=cfg,
            conn=conn,
            perception=perception,
            walker=walker,
            pkt_handler=pkt_handler,
            persona=persona,
            bus=bus,
            map_reader=map_reader,
            llm=llm,
            memory_db=memory_db,
            feed=feed,
            skill_registry=skill_registry,
        )
        avatar._forum_client = forum_client
        avatar._metrics_collector = metrics_collector
        avatar._log_sub = log_sub
        avatar._metrics_sub = metrics_sub
        return avatar

    def build_blackboard(self) -> dict:
        """Build the blackboard dict for BrainContext (legacy bridge)."""
        from anima.memory.journal import ActivityJournal

        journal = ActivityJournal(self.memory_db, agent_name=self.name)

        return {
            "persona": self.persona,
            "persona_type": self.cfg.character.persona,
            "forum_client": getattr(self, "_forum_client", None),
            "skill_registry": self.skill_registry,
            "journal": journal,
            "activity_feed": self.feed,
            "metrics": getattr(self, "_metrics_collector", None),
            "map_reader": self.map_reader,
            "bus": self.bus,
        }

    async def close(self) -> None:
        """Clean up resources."""
        if self.memory_db:
            await self.memory_db.close()
        if hasattr(self, "_log_sub"):
            self._log_sub.close()
