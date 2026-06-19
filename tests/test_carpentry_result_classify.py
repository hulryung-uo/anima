"""Regression: a material-losing failed carpentry craft must NOT count success.

ServUO 1044043 ("You failed to create the item, and some of your materials
are lost.") sets result_msg='fail' AND consumes boards. The old dispatch
``if result_msg == "success" or consumed > 0`` promoted that failure to a
+5.0 reward + fake skill gain (the same bug already fixed for blacksmithy).
The classifier must honor the explicit token.
"""

from __future__ import annotations

from anima.skills.crafting.carpentry import _classify_carpentry_result


def test_failed_craft_with_material_loss_is_fail_not_success() -> None:
    # 1044043: failed but burned boards — the bug case.
    assert _classify_carpentry_result("fail", consumed=10) == "fail"


def test_failed_craft_no_material_loss_is_fail() -> None:
    # 1044157: failed, no materials lost.
    assert _classify_carpentry_result("fail", consumed=0) == "fail"


def test_tool_broke_wins_over_consumed() -> None:
    # 1044038 worn-out tool — must surface as tool_broke even with consumption.
    assert _classify_carpentry_result("tool_broke", consumed=8) == "tool_broke"


def test_explicit_success() -> None:
    assert _classify_carpentry_result("success", consumed=12) == "success"


def test_barely_able_consumed_only_is_success() -> None:
    # quality-0 success some shards phrase without literal "you create":
    # no journal token, but boards were consumed and an item was made.
    assert _classify_carpentry_result("", consumed=12) == "success"


def test_nothing_happened_is_none() -> None:
    assert _classify_carpentry_result("", consumed=0) == "none"
