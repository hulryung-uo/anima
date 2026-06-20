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


async def test_non_object_json_payload_returns_error_not_crash() -> None:
    # ``json.loads`` returns a bare scalar/list/null for a top-level JSON value
    # that is not an object. Before the fix, ``data.get("cmd", ...)`` raised
    # AttributeError out of _handle_command (and out of the un-guarded
    # _ws_handler loop). Every non-object payload must degrade to a cmd_result.
    srv = _server()
    for raw in ('"hello"', "[1, 2]", "42", "true", "null"):
        result = await srv._handle_command(raw)
        assert result["type"] == "cmd_result", raw
        assert result["ok"] is False, raw
    # A bad payload must never latch a side effect.
    assert srv.command_bus.override_go_to is None
    assert srv.command_bus.paused is False


async def test_object_json_payload_still_dispatches() -> None:
    # The non-object guard must not break ordinary object commands.
    srv = _server()
    result = await srv._handle_command(json.dumps({"cmd": "pause"}))
    assert result["ok"] is True
    assert srv.command_bus.paused is True
