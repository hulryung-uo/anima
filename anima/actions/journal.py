"""Journal action primitive — wait for specific text in the journal."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

import structlog

from anima.actions.result import ActionResult

if TYPE_CHECKING:
    from anima.core.context import AgentContext

logger = structlog.get_logger()


async def wait_for_journal(
    ctx: AgentContext,
    patterns: list[str | re.Pattern[str]],
    timeout: float = 5.0,
    since: float | None = None,
) -> ActionResult:
    """Wait for a journal message matching any of the given patterns.

    Patterns can be plain strings (substring match) or compiled regex.
    Returns the matching text and which pattern index matched.

    Args:
        ctx: Agent context
        patterns: List of string patterns or compiled regex to match
        timeout: Max seconds to wait
        since: Only consider journal entries after this timestamp (default: now)
    """
    if since is None:
        since = time.time()

    compiled = []
    for p in patterns:
        if isinstance(p, str):
            compiled.append(re.compile(re.escape(p), re.IGNORECASE))
        else:
            compiled.append(p)

    journal = ctx.perception.social.journal

    def _check() -> tuple[int, str] | None:
        for entry in journal:
            if entry.timestamp < since:
                continue
            for i, pat in enumerate(compiled):
                if pat.search(entry.text):
                    return i, entry.text
        return None

    if ctx.bus:
        match = _check()
        if match:
            idx, text = match
            return ActionResult(success=True, data={"index": idx, "text": text})

        ok = await ctx.bus.wait_for_condition(
            lambda: _check() is not None,
            timeout=timeout,
        )
        if ok:
            match = _check()
            if match:
                idx, text = match
                return ActionResult(success=True, data={"index": idx, "text": text})

    return ActionResult(success=False, message="Journal pattern timeout")
