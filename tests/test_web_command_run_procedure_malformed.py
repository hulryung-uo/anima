"""A malformed ``run_procedure`` command must not store an invalid override.

``WebServer._ws_handler`` calls ``_handle_command`` with NO try/except around
it, so any exception raised while handling a command escapes the
``async for msg in ws`` loop and drops the client without a ``cmd_result``.
The ``run_procedure`` case guarded only with a bare ``if not name`` — a
non-empty but non-string value (``{"cmd":"run_procedure","name":["mine_ore"]}``)
slipped through and was stored verbatim as ``_override_procedure``. The planner
later does ``registry.get(name)`` (a dict lookup); an *unhashable* name (list /
dict) then raises ``TypeError: unhashable type`` deep inside the planner tick.
The same failure class the go_to / say / non-object guards exist to prevent.
"""

import json

from anima.web.command_bus import CommandBus
from anima.web.server import WebServer


def _server() -> WebServer:
    return WebServer(port=0, command_bus=CommandBus())


async def test_run_procedure_non_string_name_returns_error_not_stored() -> None:
    srv = _server()
    # Each of these is truthy, so the old ``if not name`` guard let it through.
    for bad in (123, 1.5, ["mine_ore"], {"name": "x"}, True):
        result = await srv._handle_command(
            json.dumps({"cmd": "run_procedure", "name": bad})
        )
        assert result["type"] == "cmd_result", bad
        assert result["cmd"] == "run_procedure", bad
        assert result["ok"] is False, bad
        # A rejected command must never arm the override.
        assert srv.command_bus._override_procedure is None, bad


async def test_run_procedure_blank_name_returns_error() -> None:
    srv = _server()
    for blank in ("", "   ", "\t\n"):
        result = await srv._handle_command(
            json.dumps({"cmd": "run_procedure", "name": blank})
        )
        assert result["ok"] is False, repr(blank)
        assert srv.command_bus._override_procedure is None, repr(blank)


async def test_run_procedure_missing_name_returns_error() -> None:
    srv = _server()
    result = await srv._handle_command(json.dumps({"cmd": "run_procedure"}))
    assert result["ok"] is False
    assert srv.command_bus._override_procedure is None


async def test_run_procedure_valid_name_arms_override() -> None:
    srv = _server()
    result = await srv._handle_command(
        json.dumps({"cmd": "run_procedure", "name": "mine_ore"})
    )
    assert result["ok"] is True
    # The override property is consume-once; reading it returns the armed value.
    assert srv.command_bus.override_procedure == "mine_ore"


async def test_run_procedure_unhashable_name_would_crash_registry_get() -> None:
    # Demonstrate the concrete downstream failure the guard prevents: the
    # planner force-procedure path does ``registry.get(name)`` (dict lookup),
    # which raises TypeError on an unhashable name. With the guard, that value
    # never reaches the bus, so the override stays None.
    srv = _server()
    result = await srv._handle_command(
        json.dumps({"cmd": "run_procedure", "name": ["mine_ore"]})
    )
    assert result["ok"] is False
    armed = srv.command_bus._override_procedure
    assert armed is None
    # Sanity: a dict.get with the rejected unhashable value would have raised.
    import pytest

    with pytest.raises(TypeError):
        {}.get(["mine_ore"])
