"""Action primitives — composable async packet sequences.

Each primitive sends packet(s), waits for expected response, and returns ActionResult.
Primitives are stateless and know nothing about mining or crafting — they just
execute packet sequences with proper waiting.
"""

from anima.actions.result import ActionResult

__all__ = ["ActionResult"]
