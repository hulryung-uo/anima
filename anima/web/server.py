"""Web dashboard server — serves UI and broadcasts state via WebSocket.

Uses aiohttp (already a project dependency). Runs alongside the agent
as an asyncio task. StatePublisher calls broadcast() each tick so all
connected browsers get real-time updates.

Command protocol: clients send JSON commands, server dispatches to CommandBus
and sends back cmd_result responses.

Usage:
    from anima.web.command_bus import CommandBus
    cmd_bus = CommandBus()
    server = WebServer(port=8150, command_bus=cmd_bus, conn=avatar.conn)
    asyncio.create_task(server.start())
    server.broadcast(snapshot_dict)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from aiohttp import web

from anima.web.command_bus import CommandBus

if TYPE_CHECKING:
    from anima.client.connection import UoConnection

logger = structlog.get_logger()

STATIC_DIR = Path(__file__).parent / "static"


class WebServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8150,
        command_bus: CommandBus | None = None,
        conn: UoConnection | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._cmd_bus = command_bus or CommandBus()
        self._conn = conn  # for immediate commands like say
        self._app = web.Application()
        self._clients: set[web.WebSocketResponse] = set()
        self._last_snapshot: str = "{}"
        self._setup_routes()

    @property
    def command_bus(self) -> CommandBus:
        return self._cmd_bus

    def _setup_routes(self) -> None:
        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_get("/", self._index_handler)
        self._app.router.add_static("/static", STATIC_DIR, show_index=False)

    async def _index_handler(self, request: web.Request) -> web.Response:
        index = STATIC_DIR / "index.html"
        return web.FileResponse(index)

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)

        self._clients.add(ws)
        logger.info("web_client_connected", total=len(self._clients))

        # Send current state immediately on connect
        try:
            await ws.send_str(self._last_snapshot)
        except Exception:
            pass

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    result = await self._handle_command(msg.data)
                    try:
                        await ws.send_str(json.dumps(result))
                    except Exception:
                        pass
        finally:
            self._clients.discard(ws)
            logger.info("web_client_disconnected", total=len(self._clients))

        return ws

    async def _handle_command(self, raw: str) -> dict[str, Any]:
        """Parse and dispatch a client command. Returns cmd_result dict."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"type": "cmd_result", "cmd": "?", "ok": False, "message": "invalid JSON"}

        # ``json.loads`` happily returns a bare scalar / list / null for a
        # top-level JSON value that is not an object (``"hi"`` -> str, ``[1]`` ->
        # list, ``42`` -> int, ``null`` -> None). The dispatch below immediately
        # does ``data.get("cmd", ...)``, which raises AttributeError on any of
        # those — and ``_ws_handler`` runs ``_handle_command`` with NO try/except,
        # so it escapes the ``async for`` loop, 500s the request and drops the
        # client without ever sending a cmd_result. Same failure mode the go_to
        # int-coercion guard exists to prevent; reject a non-object payload here.
        if not isinstance(data, dict):
            return {"type": "cmd_result", "cmd": "?", "ok": False,
                    "message": "command must be a JSON object"}

        cmd = data.get("cmd", "")
        logger.info("web_command", cmd=cmd, params={k: v for k, v in data.items() if k != "cmd"})

        match cmd:
            case "pause":
                self._cmd_bus.set_paused(True)
                return _ok(cmd, "Planner paused")

            case "resume":
                self._cmd_bus.set_paused(False)
                return _ok(cmd, "Planner resumed")

            case "go_to":
                # x/y come straight off the wire from an external client, so a
                # malformed value (a non-numeric string, null, a list) must not
                # escape as a ValueError/TypeError — that propagates out of the
                # un-guarded _ws_handler loop, 500s the request and drops the
                # client without ever sending a cmd_result. Coerce defensively.
                try:
                    x, y = int(data.get("x", 0)), int(data.get("y", 0))
                except (TypeError, ValueError):
                    return _err(cmd, "x and y must be integers")
                if x == 0 and y == 0:
                    return _err(cmd, "x and y required")
                self._cmd_bus._override_go_to = (x, y)
                self._cmd_bus.set_paused(False)  # auto-resume
                return _ok(cmd, f"Moving to ({x}, {y})")

            case "set_goal":
                goal = data.get("goal", "")
                self._cmd_bus.set_goal(goal or None)
                return _ok(cmd, f"Goal set to '{goal}'" if goal else "Goal cleared")

            case "run_procedure":
                name = data.get("name", "")
                if not name:
                    return _err(cmd, "procedure name required")
                self._cmd_bus._override_procedure = name
                self._cmd_bus.set_paused(False)  # auto-resume
                return _ok(cmd, f"Next tick will run '{name}'")

            case "say":
                text = data.get("text", "")
                if not text:
                    return _err(cmd, "text required")
                if not self._conn or not self._conn.connected:
                    return _err(cmd, "not connected to game server")
                from anima.client.packets import build_unicode_speech
                await self._conn.send_packet(build_unicode_speech(text))
                return _ok(cmd, f"Said: {text}")

            case _:
                return _err(cmd, f"unknown command '{cmd}'")

    def broadcast(self, snapshot: dict[str, Any]) -> None:
        """Send state snapshot to all connected WebSocket clients."""
        # Inject paused state into snapshot
        if "status" in snapshot:
            snapshot["status"]["paused"] = self._cmd_bus.paused

        data = json.dumps(snapshot)
        self._last_snapshot = data

        if not self._clients:
            return

        stale: list[web.WebSocketResponse] = []
        for ws in self._clients:
            if ws.closed:
                stale.append(ws)
                continue
            try:
                asyncio.ensure_future(ws.send_str(data))
            except Exception:
                stale.append(ws)

        for ws in stale:
            self._clients.discard(ws)

    async def start(self) -> None:
        # Browsers on localhost can carry >8KB of cookies; aiohttp's default
        # 8190-byte header-field limit then rejects the page with
        # "Header value is too long". Raise the parser limits.
        runner = web.AppRunner(
            self._app, access_log=None,
            max_line_size=32768, max_field_size=32768,
        )
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        logger.info("web_server_started", host=self.host, port=self.port)
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await runner.cleanup()


def _ok(cmd: str, message: str) -> dict[str, Any]:
    return {"type": "cmd_result", "cmd": cmd, "ok": True, "message": message}


def _err(cmd: str, message: str) -> dict[str, Any]:
    return {"type": "cmd_result", "cmd": cmd, "ok": False, "message": message}
