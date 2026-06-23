"""StatePublisher should surface pending companion goals immediately.

A companion goal can be set while the planner is busy inside a long procedure
(e.g. a pathfinding move). The command is already accepted by WebServer, but the
planner will not consume it until the procedure returns. The dashboard/bridge
state should still show the pending companion goal right away so Hermes can
verify that ``set-goal`` landed.
"""

from types import SimpleNamespace

from anima.core.bus import EventBus
from anima.monitor.state_publisher import StatePublisher
from anima.web.command_bus import CommandBus


class _SelfState:
    hits = 10
    hits_max = 10
    mana = 5
    mana_max = 5
    stam = 6
    stam_max = 6
    strength = 10
    dexterity = 10
    intelligence = 10
    x = 1
    y = 2
    z = 3
    gold = 4
    weight = 5
    weight_max = 100


def test_status_uses_pending_companion_goal_when_current_goal_not_yet_consumed() -> None:
    bus = EventBus()
    command_bus = CommandBus()
    command_bus.set_goal("Mine safely near Minoc for a few minutes.")
    seen = []
    bus.subscribe("monitor.status", lambda _topic, data: seen.append(data))
    publisher = StatePublisher(
        perception=SimpleNamespace(self_state=_SelfState()),
        blackboard={"command_bus": command_bus},
        bus=bus,
    )

    publisher._publish_status()

    assert seen[-1]["goal"] == "Mine safely near Minoc for a few minutes."


def test_status_prefers_consumed_current_goal_over_pending_command_bus_goal() -> None:
    bus = EventBus()
    command_bus = CommandBus()
    command_bus.set_goal("pending")
    seen = []
    bus.subscribe("monitor.status", lambda _topic, data: seen.append(data))
    publisher = StatePublisher(
        perception=SimpleNamespace(self_state=_SelfState()),
        blackboard={
            "command_bus": command_bus,
            "current_goal": {"description": "consumed", "source": "companion"},
        },
        bus=bus,
    )

    publisher._publish_status()

    assert seen[-1]["goal"] == "consumed"
