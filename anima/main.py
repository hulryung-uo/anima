"""Anima entry point — connect to servuo and run the behavior tree brain."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import structlog

from anima.brain.behavior_tree import BrainContext
from anima.brain.brain import Brain
from anima.brain.llm import LLMClient
from anima.client.connection import UoConnection
from anima.client.handler import PacketHandler
from anima.client.packets import (
    build_double_click,
    build_opl_request,
    build_ping,
    build_status_request,
    build_unicode_speech,
)
from anima.config import Config, load_config
from anima.data import item_name
from anima.map import MapReader
from anima.memory.database import MemoryDB
from anima.memory.journal import ActivityJournal
from anima.perception import Perception
from anima.perception.enums import Layer
from anima.perception.handlers import register_handlers
from anima.perception.walker import WalkerManager
from anima.persona import load_persona_by_name
from anima.skills.base import SkillRegistry
from anima.skills.combat.healing import HealSelf
from anima.skills.combat.melee import MeleeAttack
from anima.skills.crafting.carpentry import CraftCarpentry
from anima.skills.crafting.smelt import SmeltOre
from anima.skills.crafting.tinker import CraftTinker
from anima.skills.gathering.lumber import ChopWood
from anima.skills.gathering.mine import MineOre
from anima.skills.trade.vendor import BuyFromNpc, SellToNpc

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
)
logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Packet receive loop
# ---------------------------------------------------------------------------


async def recv_loop(conn: UoConnection, handler: PacketHandler) -> None:
    """Receive and dispatch all game packets."""
    while conn.connected:
        try:
            packet_id, data = await conn.recv_packet(timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except (ConnectionError, EOFError):
            logger.error("connection_lost")
            break

        # Ping is protocol-level — handle inline (requires I/O)
        if packet_id == 0x73:
            await conn.send_packet(build_ping(data[1] if len(data) > 1 else 0))
            continue

        # Dispatch to perception handlers
        if not handler.dispatch(packet_id, data):
            logger.debug(
                "packet_unhandled",
                packet_id=f"0x{packet_id:02X}",
                size=len(data),
            )


# ---------------------------------------------------------------------------
# Startup: inspect self
# ---------------------------------------------------------------------------


async def inspect_self(conn: UoConnection, perception: Perception) -> None:
    """Request own stats and open backpack to discover equipment/items."""
    serial = perception.self_state.serial

    await asyncio.sleep(1.0)  # let initial packets settle

    # Request full stats and skills
    await conn.send_packet(build_status_request(4, serial))
    await conn.send_packet(build_status_request(5, serial))

    # Double-click self to trigger paperdoll / equipment packets
    await conn.send_packet(build_double_click(serial))

    # Find and open backpack
    backpack_serial = perception.self_state.equipment.get(Layer.BACKPACK)
    if backpack_serial:
        await conn.send_packet(build_double_click(backpack_serial))

    await asyncio.sleep(2.0)  # wait for responses

    # Try again if backpack wasn't known yet
    if not backpack_serial:
        backpack_serial = perception.self_state.equipment.get(Layer.BACKPACK)
        if backpack_serial:
            await conn.send_packet(build_double_click(backpack_serial))
            await asyncio.sleep(1.0)

    # Log equipment
    ss = perception.self_state
    for layer, item_serial in sorted(ss.equipment.items()):
        if layer not in Layer.__members__.values():
            continue
        item = perception.world.items.get(item_serial)
        graphic = item.graphic if item else 0
        name = item_name(graphic) if graphic else ""
        logger.info(
            "equipped",
            slot=Layer(layer).name.lower(),
            name=name or f"0x{graphic:04X}",
            serial=f"0x{item_serial:08X}",
        )

    # Log backpack contents
    if backpack_serial:
        backpack_items = [
            item for item in perception.world.items.values() if item.container == backpack_serial
        ]
        for item in backpack_items:
            name = item_name(item.graphic)
            logger.info(
                "backpack_item",
                name=name or f"0x{item.graphic:04X}",
                amount=item.amount,
                serial=f"0x{item.serial:08X}",
            )
        if not backpack_items:
            logger.info("backpack_empty")
    else:
        logger.info("backpack_not_found")

    # --- Request OPL for all known entities ---
    sx, sy = ss.x, ss.y
    opl_serials = list(perception.world.opl_revisions.keys())
    for s in opl_serials:
        await conn.send_packet(build_opl_request(s))
    if opl_serials:
        await asyncio.sleep(1.5)

    # --- Nearby mobiles ---
    mobiles = perception.world.nearby_mobiles(sx, sy, distance=18)
    if mobiles:
        for mob in mobiles:
            notoriety = mob.notoriety.name.lower() if mob.notoriety else "unknown"
            dx, dy = mob.x - sx, mob.y - sy
            props = ", ".join(mob.properties[1:]) if len(mob.properties) > 1 else ""
            logger.info(
                "nearby_mobile",
                name=mob.name or f"body=0x{mob.body:04X}",
                serial=f"0x{mob.serial:08X}",
                pos=f"({mob.x},{mob.y},{mob.z})",
                dist=f"({dx:+d},{dy:+d})",
                notoriety=notoriety,
                props=props or None,
            )
    else:
        logger.info("no_nearby_mobiles")

    # --- Nearby ground items ---
    ground_items = perception.world.nearby_items(sx, sy, distance=18)
    if ground_items:
        for item in ground_items:
            name = item.name or item_name(item.graphic)
            dx, dy = item.x - sx, item.y - sy
            props = ", ".join(item.properties[1:]) if len(item.properties) > 1 else ""
            logger.info(
                "nearby_item",
                name=name or f"0x{item.graphic:04X}",
                serial=f"0x{item.serial:08X}",
                pos=f"({item.x},{item.y},{item.z})",
                dist=f"({dx:+d},{dy:+d})",
                amount=item.amount,
                props=props or None,
            )
    else:
        logger.info("no_nearby_ground_items")


# ---------------------------------------------------------------------------
# Brain loop
# ---------------------------------------------------------------------------


async def brain_loop(brain: Brain) -> None:
    """Run the behavior tree brain at ~5Hz after initial settle time."""
    await asyncio.sleep(3.0)  # wait for world to load and fastwalk keys

    # Apply stat locks immediately, skill locks after skills arrive
    persona_type = brain.context.blackboard.get("persona_type", "")
    if persona_type:
        from anima.skills.skill_manager import apply_skill_locks

        await apply_skill_locks(brain.context, persona_type)
        brain.context.blackboard["_skill_locks_pending"] = True

    # Say hello on connect
    persona_name = persona.name if persona else "Anima"
    await brain.context.conn.send_packet(build_unicode_speech(f"Hello from {persona_name}!"))
    logger.info("speech_sent", text=f"Hello from {persona_name}!")

    while brain.context.conn.connected:
        # Apply pending skill locks once skills arrive from server
        if brain.context.blackboard.get("_skill_locks_pending"):
            if brain.context.perception.self_state.skills:
                from anima.skills.skill_manager import apply_skill_locks

                pt = brain.context.blackboard.get("persona_type", "")
                if pt:
                    await apply_skill_locks(brain.context, pt)
                brain.context.blackboard["_skill_locks_pending"] = False

        await brain.tick()
        await asyncio.sleep(0.2)  # 200ms tick


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run(cfg: Config, delete_existing: bool = False) -> None:
    conn = UoConnection(timeout=cfg.client.connection_timeout)

    try:
        # Build perception + handlers BEFORE login so login-phase
        # world-state packets (0x78, 0xF3, 0xDC, 0xBF, etc.) are captured.
        perception = Perception(player_serial=0)
        walker = WalkerManager(perception.self_state, perception.events)
        pkt_handler = PacketHandler()
        register_handlers(pkt_handler, perception, walker)

        # Load persona early so we can use its name for character creation
        persona = load_persona_by_name(cfg.character.persona)

        # Use persona name as character name (config name is only an override)
        char_name = cfg.character.name
        if char_name == "Anima" and persona.name != "Anima":
            char_name = persona.name

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

        # login() already synced perception via the 0x1B handler,
        # but ensure serial is set in case perception wasn't passed
        perception.self_state.serial = result.serial

        # Load map reader for pathfinding
        resource_dir = Path(cfg.map.resource_dir).expanduser()
        map_reader: MapReader | None = None
        if resource_dir.exists():
            map_reader = MapReader(resource_dir)
            logger.info("map_reader_loaded", resource_dir=str(resource_dir))
        else:
            logger.warning("map_resource_dir_not_found", path=str(resource_dir))

        # Initialize LLM client
        llm_client = LLMClient(
            provider=cfg.llm.provider,
            model=cfg.llm.model,
            base_url=cfg.llm.base_url,
            api_key=cfg.llm.api_key,
            temperature=cfg.llm.temperature,
            timeout=cfg.llm.timeout,
        )
        logger.info(
            "llm_client_ready",
            provider=cfg.llm.provider,
            model=cfg.llm.model,
        )

        logger.info(
            "agent_ready",
            serial=f"0x{result.serial:08X}",
            position=f"({result.x}, {result.y}, {result.z})",
        )

        # Initialize memory database
        memory_db = MemoryDB(cfg.memory.db_path)
        await memory_db.init()

        logger.info("persona_loaded", name=persona.name, title=persona.title)

        # Initialize forum client if enabled
        forum_client = None
        if cfg.forum.enabled and cfg.forum.api_key:
            from anima.skills.tavern_client import TavernForumClient

            forum_client = TavernForumClient(cfg.forum.base_url, cfg.forum.api_key)
            logger.info("forum_client_ready", base_url=cfg.forum.base_url)

        # Build skill registry
        skill_registry = SkillRegistry()
        skill_registry.register(HealSelf())
        skill_registry.register(MeleeAttack())
        skill_registry.register(MineOre())
        skill_registry.register(ChopWood())
        skill_registry.register(SmeltOre())
        skill_registry.register(CraftTinker())
        skill_registry.register(CraftCarpentry())
        skill_registry.register(BuyFromNpc())
        skill_registry.register(SellToNpc())
        logger.info("skills_registered", count=len(skill_registry.all_skills))

        # Initialize activity journal
        journal = ActivityJournal(memory_db, agent_name=persona.name)
        logger.info("journal_ready", agent=persona.name)

        # Build brain with behavior tree
        brain_ctx = BrainContext(
            perception=perception,
            conn=conn,
            walker=walker,
            map_reader=map_reader,
            cfg=cfg,
            llm=llm_client,
            memory_db=memory_db,
            blackboard={
                "persona": persona,
                "persona_type": cfg.character.persona,
                "forum_client": forum_client,
                "skill_registry": skill_registry,
                "journal": journal,
            },
        )
        brain = Brain(brain_ctx)

        try:
            await asyncio.gather(
                recv_loop(conn, pkt_handler),
                inspect_self(conn, perception),
                brain_loop(brain),
            )
        finally:
            await memory_db.close()
    except ConnectionError as e:
        logger.error("connection_error", error=str(e))
    except KeyboardInterrupt:
        logger.info("shutting_down")


def main() -> None:
    parser = argparse.ArgumentParser(description="Anima — UO AI Player")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--host", default=None, help="Server host")
    parser.add_argument("--port", type=int, default=None, help="Server port")
    parser.add_argument("--user", default=None, help="Account username")
    parser.add_argument("--pass", dest="password", default=None, help="Account password")
    parser.add_argument(
        "--recreate", action="store_true", help="Delete existing character and recreate"
    )
    args = parser.parse_args()

    cfg = load_config(args.config)

    # CLI args override config file
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port
    if args.user:
        cfg.account.username = args.user
    if args.password:
        cfg.account.password = args.password

    asyncio.run(run(cfg, delete_existing=args.recreate))


if __name__ == "__main__":
    main()
