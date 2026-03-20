"""Tests for GoalManager."""

from anima.core.goals import GoalManager


def test_set_goal():
    gm = GoalManager()
    assert not gm.has_goal
    gm.set_goal("Britain Bank", 1434, 1699, "Go deposit gold")
    assert gm.has_goal
    assert gm.current.place == "Britain Bank"
    assert gm.move_target == (1434, 1699)


def test_arrive():
    gm = GoalManager()
    gm.set_goal("Bank", 100, 200)
    goal = gm.arrive()
    assert goal is not None
    assert goal.place == "Bank"
    assert not gm.has_goal
    assert gm.move_target is None


def test_abandon():
    gm = GoalManager()
    gm.set_goal("Forest", 500, 600)
    goal = gm.abandon("too far")
    assert goal.place == "Forest"
    assert not gm.has_goal


def test_stuck_count():
    gm = GoalManager()
    gm.set_goal("X", 1, 1)
    assert gm.stuck_count == 0
    gm.record_stuck()
    gm.record_stuck()
    assert gm.stuck_count == 2
    gm.arrive()
    assert gm.stuck_count == 0


def test_path_cache():
    gm = GoalManager()
    path = [(1, 2), (3, 4), (5, 6)]
    target = (5, 6)
    gm.set_path(path, target)
    assert gm.get_path(target) == path
    assert gm.get_path((99, 99)) is None  # wrong target

    gm.consume_path_step()
    assert gm.get_path(target) == [(3, 4), (5, 6)]

    gm.clear_path_cache()
    assert gm.get_path(target) is None


def test_blackboard_bridge():
    gm = GoalManager()
    gm.set_goal("Town", 10, 20, "Visit")

    bb: dict = {}
    gm.to_blackboard(bb)
    assert bb["current_goal"]["place"] == "Town"
    assert bb["move_target"] == (10, 20)

    gm2 = GoalManager()
    gm2.from_blackboard(bb)
    assert gm2.current.place == "Town"
    assert gm2.move_target == (10, 20)


def test_no_goal_blackboard():
    gm = GoalManager()
    bb: dict = {"current_goal": {"place": "X", "x": 1, "y": 2}}
    gm.from_blackboard(bb)
    assert gm.has_goal

    gm.abandon()
    gm.to_blackboard(bb)
    assert "current_goal" not in bb
