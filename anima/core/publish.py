"""Publish helper — convenience function for Avatar event publishing.

Instead of:
    feed = ctx.blackboard.get("activity_feed")
    if feed:
        feed.publish("skill", "message", importance=2)

Use:
    from anima.core.publish import pub
    pub(ctx, "action.skill", "message", importance=2)

This publishes to the EventBus AND legacy ActivityFeed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anima.brain.behavior_tree import BrainContext


def pub(
    ctx: BrainContext,
    topic: str,
    message: str = "",
    importance: int = 1,
    **data: Any,
) -> None:
    """Publish an event to EventBus and legacy ActivityFeed.

    Args:
        ctx: BrainContext (has blackboard with bus and activity_feed)
        topic: EventBus topic (e.g. "action.chop", "brain.think")
        message: Human-readable message for activity feed
        importance: 1=routine, 2=notable, 3=significant
        **data: Additional event data
    """
    event_data = {"message": message, "importance": importance, **data}

    # Publish to EventBus
    bus = ctx.blackboard.get("bus")
    if bus:
        bus.publish(topic, event_data)

    # Bridge to legacy ActivityFeed
    feed = ctx.blackboard.get("activity_feed")
    if feed and message:
        # Map topic prefix to feed category
        category = topic.split(".")[0]
        category_map = {
            "action": "skill",
            "avatar": "system",
            "brain": "brain",
            "movement": "movement",
            "social": "social",
            "combat": "combat",
            "system": "system",
            "skill": "skill",
        }
        feed_category = category_map.get(category, category)
        feed.publish(feed_category, message, importance=importance)
