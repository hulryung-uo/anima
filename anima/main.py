"""Anima entry point — connect to a UO server and run the behavior tree brain."""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from pathlib import Path

import structlog

from anima.brain.brain import Brain
from anima.client.connection import UoConnection
from anima.client.handler import PacketHandler
from anima.client.packets import (
    build_double_click,
    build_opl_request,
    build_ping,
    build_single_click,
    build_status_request,
    build_unicode_speech,
)
from anima.config import Config, load_config
from anima.core.context import AgentContext
from anima.data import item_name, mobile_display_name
from anima.perception import Perception
from anima.perception.enums import Layer

LOG_PATH = Path("data/anima.log")


def _colored_renderer() -> structlog.dev.ConsoleRenderer:
    """Build a color-coded console renderer.

    Theme auto-selected by ANIMA_THEME env var:
      ANIMA_THEME=light  → dark text on light background
      ANIMA_THEME=dark   → bright text on dark background (default)
    """
    import os

    theme = os.environ.get("ANIMA_THEME", "dark").lower()

    # ANSI codes
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"

    if theme == "light":
        # Dark colors for light backgrounds
        RED = "\033[31m"
        GREEN = "\033[32m"
        YELLOW = "\033[33m"
        BLUE = "\033[34m"
        MAGENTA = "\033[35m"
        CYAN = "\033[36m"
        GREY = "\033[90m"
        TEXT = "\033[30m"       # black text
        DIM_TEXT = "\033[90m"   # dark grey
    else:
        # Bright colors for dark backgrounds
        RED = "\033[91m"
        GREEN = "\033[92m"
        YELLOW = "\033[93m"
        BLUE = "\033[94m"
        MAGENTA = "\033[95m"
        CYAN = "\033[96m"
        GREY = "\033[90m"
        TEXT = "\033[37m"       # white text
        DIM_TEXT = "\033[90m"

    _EVENT_COLORS = {
        "planner_": CYAN,
        "procedure_": CYAN + BOLD,
        "go_to_": YELLOW,
        "door_": YELLOW,
        "mine_": GREEN,
        "smelt_": GREEN,
        "chop_": GREEN,
        "picked_up": GREEN,
        "speech": MAGENTA,
        "vendor_": MAGENTA,
        "bank_": MAGENTA,
        "skill_": BLUE,
        "equip": BLUE,
        "backpack": BLUE,
        "packet_": DIM_TEXT + DIM,
        "walk_": DIM_TEXT,
        "entity_": DIM_TEXT + DIM,
        "play_sound": DIM_TEXT + DIM,
        "play_music": DIM_TEXT + DIM,
        "game_time": DIM_TEXT + DIM,
        "weather": DIM_TEXT + DIM,
        "season": DIM_TEXT + DIM,
    }

    _LEVEL_COLORS = {
        "debug": DIM_TEXT,
        "info": TEXT,
        "warning": YELLOW + BOLD,
        "error": RED + BOLD,
        "critical": RED + BOLD,
    }

    def _render(_, __, event_dict: dict) -> str:
        ts = event_dict.pop("timestamp", "")
        level = event_dict.pop("level", "info")
        event = event_dict.pop("event", "")

        # Pick event color
        ev_color = ""
        for prefix, color in _EVENT_COLORS.items():
            if event.startswith(prefix):
                ev_color = color
                break

        # Level color overrides for warnings/errors
        lv_color = _LEVEL_COLORS.get(level, TEXT)
        if level in ("warning", "error", "critical"):
            ev_color = lv_color

        # Format key=value pairs
        kv_parts = []
        for k, v in event_dict.items():
            kv_parts.append(f"{DIM_TEXT}{k}{RESET}={CYAN}{v}{RESET}")
        kv_str = " ".join(kv_parts)

        # Timestamp dim
        ts_str = f"{DIM}{ts}{RESET}"

        # Level tag
        lv_tag = f"[{lv_color}{level:>8}{RESET}]"

        # Event name colored
        ev_str = f"{ev_color}{event}{RESET}"

        parts = [ts_str, lv_tag, ev_str]
        if kv_str:
            parts.append(kv_str)

        return " ".join(parts)

    return _render


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
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
    )


_setup_logging()
logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Packet receive loop
# ---------------------------------------------------------------------------


# If no packet arrives for this long, assume connection is dead
_RECV_DEAD_TIMEOUT = 120.0  # 2 minutes

# Ping latency tracking (ClassicUO-style: 5-slot rolling average)

class PingTracker:
    """Measures server latency using 0x73 ping packets."""

    def __init__(self) -> None:
        self._pings: list[float] = [0.0] * 5
        self._idx: int = 0
        self._send_time: float = 0.0

    @property
    def latency_ms(self) -> float:
        """Average latency in ms (non-zero pings only)."""
        vals = [p for p in self._pings if p > 0]
        return sum(vals) / len(vals) if vals else 0.0

    def on_send(self) -> int:
        """Record send time, return seq to use."""
        self._send_time = asyncio.get_event_loop().time()
        seq = self._idx % 5
        self._idx = (self._idx + 1) % 5
        return seq

    def on_recv(self, seq: int) -> None:
        """Record received pong."""
        if self._send_time > 0:
            ms = (asyncio.get_event_loop().time() - self._send_time) * 1000
            self._pings[seq % 5] = ms


_ping_tracker = PingTracker()


async def ping_loop(conn: UoConnection) -> None:
    """Send periodic pings to measure server latency (every 5s)."""
    while conn.connected:
        await asyncio.sleep(5.0)
        if not conn.connected:
            break
        seq = _ping_tracker.on_send()
        try:
            await conn.send_packet(build_ping(seq))
        except Exception:
            break


async def recv_loop(conn: UoConnection, handler: PacketHandler) -> None:
    """Receive and dispatch all game packets."""
    last_packet = asyncio.get_event_loop().time()

    while conn.connected:
        try:
            packet_id, data = await conn.recv_packet(timeout=1.0)
        except asyncio.TimeoutError:
            # Check for dead connection — server sends pings every ~30s
            now = asyncio.get_event_loop().time()
            if now - last_packet > _RECV_DEAD_TIMEOUT:
                logger.error("connection_dead", silence_s=now - last_packet)
                break
            continue
        except (ConnectionError, EOFError):
            logger.error("connection_lost")
            break

        last_packet = asyncio.get_event_loop().time()

        # Ping response — measure latency
        if packet_id == 0x73:
            seq = data[1] if len(data) > 1 else 0
            _ping_tracker.on_recv(seq)
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

BACKPACK_GRAPHICS = {0x0E75, 0x0E76, 0x09B2}


def _find_player_backpack(items, player_serial: int) -> int | None:
    """Find the player's backpack serial from world items.

    Strict: must be equipped on the player (container == player_serial)
    AND at BACKPACK layer (0x15) AND have a backpack graphic.

    The AND on `container == player_serial` is critical — every nearby
    mobile (NPC, animal, other players) has a backpack at layer 0x15 on
    their own body. Using OR (the previous bug) would match the closest
    NPC's backpack when the login position is inside a shop, causing the
    agent to think its own backpack is empty.
    """
    for it in items.values():
        if (it.graphic in BACKPACK_GRAPHICS
                and it.container == player_serial
                and it.layer == 0x15):
            return it.serial
    return None


async def inspect_self(conn: UoConnection, perception: Perception) -> None:
    """Request own stats and open backpack to discover equipment/items."""
    serial = perception.self_state.serial

    await asyncio.sleep(3.0)  # let server send 0x78 MobileIncoming with equipment

    # Check if equipment already arrived from 0x78 during login
    if perception.self_state.equipment.get(Layer.BACKPACK):
        logger.info("backpack_from_login", serial=f"0x{perception.self_state.equipment[Layer.BACKPACK]:08X}")

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

    # Retry — equipment may arrive late
    for attempt in range(5):
        backpack_serial = perception.self_state.equipment.get(Layer.BACKPACK)
        if backpack_serial:
            await conn.send_packet(build_double_click(backpack_serial))
            await asyncio.sleep(1.0)
            break

        # Fallback 1: scan world items for equipment on our character
        for it in perception.world.items.values():
            if it.container == serial and it.layer > 0:
                perception.self_state.equipment[it.layer] = it.serial

        backpack_serial = perception.self_state.equipment.get(Layer.BACKPACK)
        if backpack_serial:
            logger.info("backpack_found_from_world_items", serial=f"0x{backpack_serial:08X}")
            await conn.send_packet(build_double_click(backpack_serial))
            await asyncio.sleep(1.0)
            break

        # Fallback 2: try known backpack serial pattern
        # In UO, backpack is often created right after the character.
        # Scan container content requests to find it.
        if attempt >= 2:
            # Request stats which sometimes triggers equipment send
            await conn.send_packet(build_status_request(4, serial))
            await asyncio.sleep(0.5)

        # Re-request equipment
        logger.debug("inspect_self_retry", attempt=attempt + 1)
        await conn.send_packet(build_double_click(serial))
        await asyncio.sleep(2.0)

    # Fallback: strict graphic scan — look for a backpack equipped on the
    # player specifically. MUST check `container == serial` — see
    # _find_player_backpack docstring for why OR-ing with layer==0x15 is
    # a bug (matches nearby NPC backpacks).
    if not perception.self_state.equipment.get(Layer.BACKPACK):
        bp_serial = _find_player_backpack(perception.world.items, serial)
        if bp_serial is not None:
            perception.self_state.equipment[Layer.BACKPACK] = bp_serial
            logger.info("backpack_found_by_graphic", serial=f"0x{bp_serial:08X}")
            await conn.send_packet(build_double_click(bp_serial))
            await asyncio.sleep(1.0)

    # Last resort: cached serial + brute-force probe
    # Cache is more reliable than any heuristic when it exists, so we try
    # it here. Note: the probe only accepts a candidate when items_in is
    # non-empty, so a stale cache with a now-deleted serial is rejected.
    if not perception.self_state.equipment.get(Layer.BACKPACK):
        cache_file = Path("data/backpack_cache.json")
        cached_serial = None
        if cache_file.exists():
            try:
                import json as _json
                cache = _json.loads(cache_file.read_text())
                if cache.get("player_serial") == serial:
                    cached_serial = cache.get("backpack_serial")
            except Exception:
                pass

        candidates = []
        if cached_serial:
            candidates.append(cached_serial)
        candidates.extend([
            serial | 0x40000000,
            (serial | 0x40000000) + 1,
        ])
        # ITEM serials live at >= 0x40000000; a mobile-range candidate
        # (e.g. serial+1) can "match" because a nearby mob's worn equipment
        # has container == mob serial — that mis-bound the backpack to a
        # HeadlessOne once. Only probe item-range serials.
        candidates = [c for c in candidates if c >= 0x40000000]

        for bp_candidate in candidates:
            logger.debug("backpack_brute_try", serial=f"0x{bp_candidate:08X}")
            await conn.send_packet(build_double_click(bp_candidate))
            await asyncio.sleep(1.0)
            items_in = [it for it in perception.world.items.values() if it.container == bp_candidate]
            if items_in:
                perception.self_state.equipment[Layer.BACKPACK] = bp_candidate
                logger.info("backpack_found_brute", serial=f"0x{bp_candidate:08X}", items=len(items_in))
                break

    # Save backpack serial to cache for next session
    bp_final = perception.self_state.equipment.get(Layer.BACKPACK)
    if bp_final:
        try:
            import json as _json
            cache_file = Path("data/backpack_cache.json")
            cache_file.write_text(_json.dumps({
                "player_serial": serial,
                "backpack_serial": bp_final,
            }))
        except Exception:
            pass

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

    # Log backpack contents (re-read from equipment — brute-force may have updated it)
    backpack_serial = perception.self_state.equipment.get(Layer.BACKPACK)
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

    # --- Request OPL for all known entities + nearby mobiles ---
    sx, sy = ss.x, ss.y
    opl_serials = set(perception.world.opl_revisions.keys())
    # Also request OPL for nearby mobiles (to get NPC titles)
    for mob in perception.world.nearby_mobiles(sx, sy, distance=18):
        opl_serials.add(mob.serial)
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
                name=mobile_display_name(mob),
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


async def _reap_task(task: asyncio.Task) -> None:
    """Cancel a background task and AWAIT it to a real terminal state.

    A bare ``task.cancel()`` only *requests* cancellation: the task keeps
    running detached until the loop next schedules it, and the planner's
    ``finally`` returns immediately. The reconnect loop then proceeds to
    ``avatar.close()`` (tearing down the TCP connection / LLM) while the
    forum task may still be suspended mid-``await`` on ``forum_client.
    create_post`` (HTTP) or ``ctx.llm.chat`` — so it can issue a stray
    network/LLM call against a half-torn-down world, and its unretrieved
    CancelledError/exception trips asyncio's "Task was destroyed but it is
    pending" / "Task exception was never retrieved" warnings.

    Awaiting the cancelled task makes shutdown deterministic: the task is
    fully unwound before we return. Mirrors MetaController.close() /
    StrategySelector background-task reaping (commits a3956e4, 3142ad1).
    Idempotent and never raises.
    """
    if task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("forum_task_reap_error", error=str(e))


async def planner_loop(ctx: AgentContext, start_delay_s: float = 0.0,
                       warp_hold_s: float = 0.0) -> None:
    """Run the v2 rule-based planner instead of behavior tree brain."""
    from anima.planner.planner import Planner

    if warp_hold_s > 0:
        # Foundry eval harness: hold the first planning tick until the GM
        # fixed-start TELEPORT actually lands (position jumps >20 tiles from
        # where we logged in), or the timeout expires. Unlike a fixed delay
        # this is robust to GM queueing under parallel evals.
        ss = ctx.perception.self_state
        # wait for a real position first (login burst)
        t0 = asyncio.get_running_loop().time()
        while (ss.x, ss.y) == (0, 0) and asyncio.get_running_loop().time() - t0 < 30:
            await asyncio.sleep(0.2)
        ox, oy = ss.x, ss.y
        logger.info("planner_warp_hold", max_s=warp_hold_s, origin=(ox, oy))
        t0 = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - t0 < warp_hold_s:
            if max(abs(ss.x - ox), abs(ss.y - oy)) > 20:
                logger.info("planner_warp_detected", pos=(ss.x, ss.y))
                await asyncio.sleep(1.0)   # let the post-warp burst settle
                break
            await asyncio.sleep(0.3)
        else:
            logger.warning("planner_warp_hold_timeout")
    elif start_delay_s > 0:
        # Fixed delay variant (kept for compatibility).
        logger.info("planner_start_delay", seconds=start_delay_s)
        await asyncio.sleep(start_delay_s)

    # Wait until equipment is loaded (backpack available)
    for _ in range(60):  # up to 30 seconds
        await asyncio.sleep(0.5)
        if ctx.perception.self_state.equipment.get(0x15):
            break
    else:
        logger.warning("planner_no_backpack_after_wait")

    # Apply skill locks
    persona_type = ctx.blackboard.get("persona_type", "")
    if persona_type:
        from anima.skills.skill_manager import apply_skill_locks
        await apply_skill_locks(ctx, persona_type)

    # Say hello
    persona_name = ctx.persona.name if ctx.persona else "Anima"
    await ctx.conn.send_packet(build_unicode_speech(f"Hello from {persona_name}!"))
    logger.info("planner_started", agent=persona_name)

    if not ctx.blackboard.get("procedure_registry"):
        logger.error("planner_no_registry")
        return

    registry = ctx.blackboard["procedure_registry"]
    command_bus = ctx.blackboard.get("command_bus")
    planner = Planner(registry, command_bus=command_bus)

    # Run planner + forum posting in parallel
    # Forum runs as a background task — errors don't kill the planner
    forum_task = asyncio.create_task(_forum_loop(ctx))
    try:
        await planner.run(ctx)
    finally:
        await _reap_task(forum_task)


FORUM_INTERVAL = 3600  # minimum 1 hour between posts
FORUM_LAST_POST_FILE = Path("data/forum_last_post.txt")


async def _forum_loop(ctx: AgentContext) -> None:
    """Post to uotavern.com forum periodically with activity summary."""
    import time as _time

    forum_client = ctx.forum_client
    if not forum_client:
        logger.info("forum_disabled", reason="no forum client configured")
        return

    persona_name = ctx.persona.name if ctx.persona else "Anima"

    # Check when last post was made (survives restarts)
    last_post_time = 0.0
    if FORUM_LAST_POST_FILE.exists():
        try:
            last_post_time = float(FORUM_LAST_POST_FILE.read_text().strip())
        except Exception:
            pass

    while ctx.conn.connected:
        now = _time.time()
        since_last = now - last_post_time
        if since_last < FORUM_INTERVAL:
            wait = FORUM_INTERVAL - since_last
            logger.debug("forum_cooldown", wait=f"{wait:.0f}s")
            await asyncio.sleep(min(wait + 10, 300))  # check every 5 min max
            continue

        # Wait a bit after startup to accumulate activity
        if last_post_time == 0:
            await asyncio.sleep(120)  # 2 min initial delay on first ever post

        try:
            summary = await _build_activity_summary(ctx, persona_name)
            title = summary["title"]
            body = summary["body"]

            post_id = await forum_client.create_post(title, body, "tavern")
            if post_id:
                last_post_time = _time.time()
                FORUM_LAST_POST_FILE.write_text(str(last_post_time))
                logger.info("forum_posted", post_id=post_id, title=title)
                if ctx.bus:
                    ctx.bus.publish("social.forum_post", {
                        "message": f"📝 Posted: {title}",
                        "importance": 2,
                    })
            else:
                logger.warning("forum_post_failed", title=title)
        except Exception as e:
            logger.warning("forum_error", error=str(e))

        await asyncio.sleep(60)  # check again in 1 min


async def _build_activity_summary(ctx: AgentContext, persona_name: str) -> dict:
    """Build a forum post with a narrative story about what the agent did."""
    import json
    import time
    from datetime import datetime

    ss = ctx.perception.self_state
    now = datetime.now()

    # Gather raw data for LLM
    stats_lines = []
    try:
        import sqlite3
        db_path = Path("data/anima.db")
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            cutoff = time.time() - FORUM_INTERVAL
            rows = conn.execute("""
                SELECT procedure, result, COUNT(*) as cnt
                FROM action_logs WHERE timestamp > ?
                GROUP BY procedure, result ORDER BY cnt DESC
            """, (cutoff,)).fetchall()
            conn.close()
            for proc, result, cnt in rows[:10]:
                stats_lines.append(f"{proc} {result}: {cnt}x")
    except Exception:
        pass

    activity_lines = []
    try:
        state_file = Path("data/state.json")
        if state_file.exists():
            state = json.loads(state_file.read_text())
            seen = set()
            for a in state.get("activity", [])[-15:]:
                msg = a.get("message", "")
                key = msg[:25]
                if key not in seen:
                    seen.add(key)
                    activity_lines.append(msg)
    except Exception:
        pass

    skill_names = {45: "Mining", 7: "Blacksmithy", 37: "Tinkering",
                   44: "Lumberjacking", 11: "Carpentry"}
    top_skills = sorted(ss.skills.values(), key=lambda s: -s.value)[:3]
    skills_text = ", ".join(
        f"{skill_names.get(s.id, f'Skill#{s.id}')} {s.value:.1f}"
        for s in top_skills
    )

    raw_data = f"""Character: {persona_name}
Position: ({ss.x}, {ss.y})
Gold: {ss.gold}, Weight: {ss.weight}/{ss.weight_max}
Skills: {skills_text}
Procedure stats (last hour): {'; '.join(stats_lines) if stats_lines else 'none'}
Recent events: {'; '.join(activity_lines[-8:]) if activity_lines else 'nothing notable'}"""

    # Use LLM to write a narrative post
    if ctx.llm:
        try:
            prompt = f"""You are {persona_name}, a miner and blacksmith in Ultima Online.
Write a tavern journal entry with a real story — not a status update.

Requirements:
- 3-4 paragraphs (roughly 200-350 words total)
- First person, conversational, like telling the tale to drinkers at the tavern
- Build a narrative arc: set the scene, introduce a specific moment or struggle,
  reflect on what it meant, and hint at what you'll do next
- Pick ONE specific moment from the events below and dwell on it — the feel of
  the pickaxe hitting stone, the warmth of the forge, the frustration of a
  broken tongs, the relief of selling to a familiar vendor, the weight of
  ingots in your pack
- Use sensory details, small observations, inner monologue
- Mention specific places by name (Minoc, the mine forge, the blacksmith)
- Avoid listing stats or bullet points — weave numbers into the narrative
  naturally ("nearly fifty ingots in my pack" not "49 iron ingots")
- Don't start every paragraph the same way
- No headers, no lists, no markdown formatting — just flowing prose

Here's what actually happened in this hour:
{raw_data}

Write ONLY the journal entry, nothing else. Make it feel lived-in."""

            response = await ctx.llm.chat([
                {"role": "user", "content": prompt},
            ])
            if response and response.text and len(response.text) > 20:
                title = f"{persona_name}'s Journal — {now.strftime('%b %d')}"
                return {"title": title, "body": response.text.strip()}
        except Exception as e:
            logger.warning("forum_llm_failed", error=str(e))

    # Fallback: simple summary if LLM fails
    title = f"{persona_name}'s Log — {now.strftime('%b %d, %H:%M')}"
    body = "Been working the mines near Minoc. "
    if stats_lines:
        body += f"Today's work: {', '.join(stats_lines[:4])}. "
    body += f"Currently at ({ss.x}, {ss.y}) with {ss.gold} gold."
    return {"title": title, "body": body}


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

        # Request names for nearby unnamed NPCs
        # Try OPL first (AOS+ servers), fallback to single-click (UOR servers)
        ss = brain.context.perception.self_state
        for mob in brain.context.perception.world.nearby_mobiles(ss.x, ss.y, distance=18):
            if not mob.name and mob.serial != ss.serial:
                await brain.context.conn.send_packet(build_opl_request(mob.serial))
                await brain.context.conn.send_packet(build_single_click(mob.serial))

        await brain.tick()
        await asyncio.sleep(0.2)  # 200ms tick


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


RECONNECT_DELAY = 10.0  # seconds between reconnect attempts
MAX_RECONNECT_DELAY = 300.0  # max backoff


async def run(
    cfg: Config,
    delete_existing: bool = False,
    use_planner: bool = True,
    web_port: int = 8150,
    planner_delay_s: float = 0.0,
    planner_warp_hold_s: float = 0.0,
) -> None:
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

            # Build typed context (v2 migration Step 1)
            brain_ctx = avatar.build_context()
            # Store procedure_registry in blackboard for planner access
            if avatar.procedure_registry:
                brain_ctx.blackboard["procedure_registry"] = avatar.procedure_registry
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

            # API server — always runs, clients (TUI/Web/agents) connect via WebSocket
            from anima.web.command_bus import CommandBus
            from anima.web.server import WebServer
            cmd_bus = CommandBus()
            api_server = WebServer(
                port=web_port, command_bus=cmd_bus, conn=avatar.conn,
            )
            brain_ctx.blackboard["command_bus"] = cmd_bus

            state_pub = StatePublisher(
                avatar.perception, brain_ctx.blackboard, avatar.bus,
                map_reader=avatar.map_reader,
                web_broadcast=api_server.broadcast,
            )

            # Select brain engine: v2 planner or legacy behavior tree
            if use_planner:
                engine_coro = planner_loop(brain_ctx, start_delay_s=planner_delay_s,
                                           warp_hold_s=planner_warp_hold_s)
                logger.info("engine_mode", mode="v2_planner")
            else:
                engine_coro = brain_loop(brain)
                logger.info("engine_mode", mode="v1_brain")

            game_coros: list = [
                recv_loop(avatar.conn, avatar.pkt_handler),
                inspect_self(avatar.conn, avatar.perception),
                engine_coro,
                state_pub.run(interval=0.5),
                ping_loop(avatar.conn),
                api_server.start(),
            ]

            try:
                # Use TaskGroup so that if ANY coro exits (e.g. recv_loop
                # on disconnect), all others are cancelled → triggers reconnect.
                async with asyncio.TaskGroup() as tg:
                    for coro in game_coros:
                        tg.create_task(coro)
            except* ConnectionError as eg:
                logger.error("connection_lost_in_task", errors=[str(e) for e in eg.exceptions])
            except* asyncio.CancelledError:
                logger.info("tasks_cancelled")
            except* Exception as eg:
                logger.error("task_crashed", errors=[str(e) for e in eg.exceptions])
            finally:
                await avatar.close()

            # If we get here, reconnect
            logger.warning("session_ended", reason="task group exited")

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
        "--legacy", action="store_true", help="Use legacy v1 behavior tree instead of v2 planner"
    )
    parser.add_argument(
        "--web-port", type=int, default=8150, help="API server port for dashboard/TUI"
    )
    parser.add_argument(
        "--planner-delay", type=float, default=0.0,
        help="Seconds to hold the first planner tick (Foundry eval fixed-start)",
    )
    parser.add_argument(
        "--planner-warp-hold", type=float, default=0.0,
        help="Hold the first planner tick until a >20-tile teleport lands, "
             "or this many seconds pass (robust under queued GM setups)",
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

    asyncio.run(run(
        cfg,
        delete_existing=args.recreate,
        use_planner=not args.legacy,
        planner_delay_s=args.planner_delay,
        planner_warp_hold_s=args.planner_warp_hold,
        web_port=args.web_port,
    ))


if __name__ == "__main__":
    main()
