"""A malformed ``say`` command must not crash the websocket command handler.

``WebServer._ws_handler`` calls ``_handle_command`` with NO try/except around
it, so any exception raised while handling a command escapes the
``async for msg in ws`` loop, 500s the aiohttp request and disconnects the
client without ever sending back a ``cmd_result``. The ``say`` case guarded
only with a bare ``if not text`` — a non-empty but non-string value
(``{"cmd":"say","text":123}``) slipped through to ``build_unicode_speech``,
whose first op is ``text.strip()`` -> ``AttributeError: 'int' object has no
attribute 'strip'``. Exactly the kind of value an external (TUI / web / agent)
client can put on the wire, and the same failure mode the go_to / non-object
guards exist to prevent.
"""

import json

from anima.web.command_bus import CommandBus
from anima.web.server import WebServer


class _FakeConn:
    connected = True

    def __init__(self) -> None:
        self.sent = 0

    async def send_packet(self, _packet: bytes) -> None:
        self.sent += 1


def _server(conn: _FakeConn | None = None) -> WebServer:
    return WebServer(port=0, command_bus=CommandBus(), conn=conn)


async def test_say_non_string_text_returns_error_not_crash() -> None:
    conn = _FakeConn()
    srv = _server(conn)
    # Before the fix each of these raised AttributeError (text.strip()) out of
    # _handle_command and out of the un-guarded _ws_handler loop.
    for bad in (123, 1.5, ["hi"], {"a": 1}, True):
        result = await srv._handle_command(json.dumps({"cmd": "say", "text": bad}))
        assert result["type"] == "cmd_result", bad
        assert result["cmd"] == "say", bad
        assert result["ok"] is False, bad
    # A malformed say must never have spoken to the server.
    assert conn.sent == 0


async def test_say_empty_or_whitespace_text_returns_error() -> None:
    conn = _FakeConn()
    srv = _server(conn)
    for blank in ("", "   ", "\t\n"):
        result = await srv._handle_command(json.dumps({"cmd": "say", "text": blank}))
        assert result["ok"] is False, repr(blank)
    assert conn.sent == 0


async def test_say_valid_text_still_speaks() -> None:
    conn = _FakeConn()
    srv = _server(conn)
    result = await srv._handle_command(json.dumps({"cmd": "say", "text": "hello there"}))
    assert result["ok"] is True
    assert conn.sent == 1
