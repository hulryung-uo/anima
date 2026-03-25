# TODOS

## ActionLog retention policy
- **What**: Prune old ActionLog entries (12-30 writes/min → 500K+ rows/month)
- **Why**: Dead weight — recency-bounded queries ignore old data
- **Context**: Background daily task, keep last N days. Not blocking for migration.
- **Depends on**: ActionLog table (Step 3)

## Silent failure handling (3 critical gaps)
- **What**: Add error handling for pathfinding no-path, ActionLog write failure, unrecognized journal text
- **Why**: Prevents "agent does nothing" debugging mysteries
- **Context**: ~15 lines total defensive code across Steps 2-4

## Planner minimum delay
- **What**: `await asyncio.sleep(0.2)` after each planner loop iteration
- **Why**: Prevents CPU spin-loop on rapid procedure failures
- **Context**: 3 lines in planner loop (Step 5)
