"""Anima entry point — connect to servuo and run the behavior tree brain."""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from pathlib import Path

import structlog

from anima.brain.behavior_tree import BrainContext
from anima.brain.brain import Brain
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
from anima.perception import Perception
from anima.perception.enums import Layer

LOG_PATH = Path("data/anima.log")


def _setup_logging() -> None:
    """Configure structlog to write to both console and data/anima.log."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    # stdlib file handler — structlog will route through it
    file_handler = logging.FileHandler(str(LOG_PATH), encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Suppress noisy third-party debug output that floods the log
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    logging.getLogger("litellm").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
    )


_setup_logging()
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
    persona = brain.context.blackboard.get("persona")
    persona_name = persona.name if persona else "Anima"
    await brain.context.conn.send_packet(build_unicode_speech(f"Hello from {persona_name}!"))
    logger.info("speech_sent", text=f"Hello from {persona_name}!")

    # Periodic analysis setup
    from anima.monitor.analyzer import analyze, generate_report, save_report
    last_analysis = time.time()
    ANALYSIS_INTERVAL = 600.0  # 10 minutes

    while brain.context.conn.connected:
        # Apply pending skill locks once skills arrive from server
        if brain.context.blackboard.get("_skill_locks_pending"):
            if brain.context.perception.self_state.skills:
                from anima.skills.skill_manager import apply_skill_locks

                pt = brain.context.blackboard.get("persona_type", "")
                if pt:
                    await apply_skill_locks(brain.context, pt)
                brain.context.blackboard["_skill_locks_pending"] = False

        # Periodic analysis (every 10 minutes)
        now = time.time()
        if now - last_analysis >= ANALYSIS_INTERVAL:
            last_analysis = now
            try:
                mc = brain.context.blackboard.get("metrics")
                if mc:
                    window = mc.get_window()
                    problems = analyze(window)
                    report = generate_report(
                        window, problems,
                        agent_name=persona_name,
                    )
                    path = save_report(report, agent_name=persona_name)
                    logger.info("analysis_saved", path=str(path))
                    feed = brain.context.blackboard.get("activity_feed")
                    if feed:
                        severity = max(
                            (p.severity for p in problems),
                            key=lambda s: ["LOW", "MEDIUM", "HIGH", "CRITICAL"].index(s),
                            default="LOW",
                        )
                        feed.publish(
                            "system",
                            f"Analysis: {severity} — {len(problems)} issue(s)",
                            importance=2 if severity in ("HIGH", "CRITICAL") else 1,
                        )
            except Exception as e:
                logger.warning("analysis_error", error=str(e))

        # Check for shutdown request — write final forum post
        shutdown_ev = brain.context.blackboard.get("shutdown_event")
        if brain.context.blackboard.get("shutdown_requested") or (
            shutdown_ev and shutdown_ev.is_set()
        ):
            logger.info("shutdown_writing_final_post")
            try:
                from anima.skills.forum_action import forum_write_action
                brain.context.blackboard["forum_last_post"] = 0.0
                await forum_write_action(brain.context)
            except Exception:
                pass
            break

        await brain.tick()
        await asyncio.sleep(0.2)  # 200ms tick


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


RECONNECT_DELAY = 10.0  # seconds between reconnect attempts
MAX_RECONNECT_DELAY = 300.0  # max backoff


async def run(cfg: Config, delete_existing: bool = False, enable_tui: bool = False) -> None:
    from anima.core.avatar import Avatar

    delay = RECONNECT_DELAY
    first_run = True

    while True:
        try:
            logger.info(
                "connecting",
                host=cfg.server.host,
                port=cfg.server.port,
                reconnect=not first_run,
            )
            avatar = await Avatar.create(
                cfg, delete_existing=delete_existing if first_run else False,
            )
            first_run = False
            delay = RECONNECT_DELAY  # reset backoff on success

            # Build brain with behavior tree (legacy bridge via blackboard)
            blackboard = avatar.build_blackboard()
            brain_ctx = BrainContext(
                perception=avatar.perception,
                conn=avatar.conn,
                walker=avatar.walker,
                map_reader=avatar.map_reader,
                cfg=avatar.cfg,
                llm=avatar.llm,
                memory_db=avatar.memory_db,
                blackboard=blackboard,
            )
            brain = Brain(brain_ctx)

            # Bridge skill_change events → activity feed
            def _on_skill_change(topic: str, data: dict) -> None:
                diff = data.get("diff", 0)
                if abs(diff) >= 0.1 and avatar.feed:
                    name = data.get("name", "?")
                    val = data.get("value", 0)
                    arrow = "\u2191" if diff > 0 else "\u2193"
                    avatar.feed.publish(
                        "skill",
                        f"{arrow} {name} → {val:.1f} ({diff:+.1f})",
                        importance=2,
                    )

            avatar.bus.subscribe("avatar.skill_change", _on_skill_change)

            if avatar.feed:
                avatar.feed.publish(
                    "system",
                    f"{avatar.name} connected to {cfg.server.host}:{cfg.server.port}",
                    importance=2,
                )

            # State publisher — feeds monitor subscribers via EventBus
            from anima.monitor.state_publisher import StatePublisher

            state_pub = StatePublisher(avatar.perception, blackboard, avatar.bus)

            game_coros: list = [
                recv_loop(avatar.conn, avatar.pkt_handler),
                inspect_self(avatar.conn, avatar.perception),
                brain_loop(brain),
                state_pub.run(interval=0.5),
            ]

            # TUI monitor — subscribes to EventBus
            if enable_tui:
                from anima.monitor.tui import AnimaMonitor

                shutdown_event = asyncio.Event()
                blackboard["shutdown_event"] = shutdown_event
                monitor = AnimaMonitor(
                    bus=avatar.bus,
                    map_reader=avatar.map_reader,
                    shutdown_event=shutdown_event,
                )
                game_coros.append(monitor.run())

            try:
                await asyncio.gather(*game_coros)
            finally:
                await avatar.close()

            # If we get here normally (all coros finished), reconnect
            logger.warning("session_ended", reason="all tasks finished")

        except KeyboardInterrupt:
            logger.info("shutting_down")
            return
        except ConnectionError as e:
            logger.error("connection_lost", error=str(e))
        except Exception as e:
            logger.error("unexpected_error", error=str(e), type=type(e).__name__)

        logger.info("reconnecting", delay=delay)
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, MAX_RECONNECT_DELAY)


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
    parser.add_argument(
        "--tui", action="store_true", help="Enable TUI monitor (subscribes to EventBus)"
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

    asyncio.run(run(cfg, delete_existing=args.recreate, enable_tui=args.tui))


if __name__ == "__main__":
    main()
