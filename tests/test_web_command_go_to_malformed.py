"""A malformed go_to command must not crash the websocket command handler.

``WebServer._ws_handler`` calls ``_handle_command`` with NO try/except around
it, so any exception raised while parsing a command escapes the
``async for msg in ws`` loop, 500s the aiohttp request and disconnects the
client without ever sending back a ``cmd_result``. The ``go_to`` case used to
do a bare ``int(data.get("x", 0))``, which raises ``ValueError`` on a
non-numeric string and ``TypeError`` on ``None``/list — exactly the kind of
value an external (TUI / web / agent) client can put on the wire.
"""

import json

from anima.web.command_bus import CommandBus
from anima.web.server import WebServer


def _server() -> WebServer:
    return WebServer(port=0, command_bus=CommandBus(), conn=None)


async def test_go_to_non_numeric_x_returns_error_not_crash() -> None:
    srv = _server()
    # Before the fix this raised ValueError out of _handle_command.
    result = await srv._handle_command(json.dumps({"cmd": "go_to", "x": "abc", "y": 5}))
    assert result["type"] == "cmd_result"
    assert result["cmd"] == "go_to"
    assert result["ok"] is False
    # No override should have been latched from the bad command.
    assert srv.command_bus.override_go_to is None


async def test_go_to_null_coords_returns_error_not_crash() -> None:
    srv = _server()
    # Before the fix this raised TypeError (int(None)) out of _handle_command.
    result = await srv._handle_command(json.dumps({"cmd": "go_to", "x": None, "y": None}))
    assert result["ok"] is False
    assert srv.command_bus.override_go_to is None


async def test_go_to_valid_coords_still_latches_override() -> None:
    srv = _server()
    result = await srv._handle_command(json.dumps({"cmd": "go_to", "x": 100, "y": 200}))
    assert result["ok"] is True
    # override_go_to is read-once (clears on read) — capture it once.
    assert srv.command_bus.override_go_to == (100, 200)


async def test_go_to_numeric_string_coords_accepted() -> None:
    srv = _server()
    # int("100") is valid — a numeric string should still work.
    result = await srv._handle_command(json.dumps({"cmd": "go_to", "x": "100", "y": "200"}))
    assert result["ok"] is True
    assert srv.command_bus.override_go_to == (100, 200)
