# Self-Improvement System Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 6 interconnected issues in the self-improvement system so that it correctly detects problems, communicates fixes to Claude Code within timeout, persists state across restarts, and signals the planner to skip broken procedures.

**Architecture:** Three files are modified: `tools/self_improve.py` (problem detection from DB stats), `tools/diagnose.py` (prompt size + streak detection), `tools/supervisor.py` (timeout strategy, fix persistence, planner hints). One new file: `data/supervisor_hints.json` written by supervisor, read by planner. Planner (`anima/planner/planner.py`) reads the hints file to skip procedures that supervisor gave up fixing.

**Tech Stack:** Python 3.12+, sqlite3, json, pytest

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `tools/self_improve.py` | Modify | Add DB-based failure detection to `detect_problems()` |
| `tools/diagnose.py` | Modify | Fix streak detection (no LIMIT, ASC order), reduce prompt sizes |
| `tools/supervisor.py` | Modify | Progressive timeout, persist fix_attempts, write supervisor hints |
| `anima/planner/planner.py` | Modify | Read supervisor hints in `select_procedure()` |
| `tests/test_self_improve.py` | Create | Tests for DB-based problem detection |
| `tests/test_supervisor.py` | Create | Tests for fix attempt persistence, progressive timeout, hints writing |
| `tests/test_planner.py` | Modify | Test supervisor hints integration |

---

### Task 1: DB-Based Problem Detection in `detect_problems()`

**Files:**
- Create: `tests/test_self_improve.py`
- Modify: `tools/self_improve.py:216-428` (`detect_problems()`)

The core bug: `detect_problems()` only looks at log-text counters but ignores the `db_stats` dict that contains actual procedure success/fail counts from the DB.

- [ ] **Step 1: Write failing tests for DB-based detection**

Create `tests/test_self_improve.py`:

```python
"""Tests for self_improve.detect_problems — DB-based failure detection."""
from tools.self_improve import detect_problems


class TestDetectProblemsFromDB:
    def test_high_fail_rate_detected(self):
        """Procedure with >80% fail rate and >10 attempts → HIGH problem."""
        data = {
            "counts": {},
            "recent_lines": 100,
            "db_stats": {
                "craft_blacksmith:missing_resource": {"count": 54, "avg_ms": 8000},
                "craft_blacksmith:success": {"count": 0, "avg_ms": 0},
            },
        }
        problems = detect_problems(data)
        names = [p["name"] for p in problems]
        assert "db_procedure_failing" in names
        match = [p for p in problems if p["name"] == "db_procedure_failing"][0]
        assert match["severity"] in ("HIGH", "CRITICAL")

    def test_moderate_fail_rate_ignored(self):
        """Procedure with 50% fail rate → no problem."""
        data = {
            "counts": {},
            "recent_lines": 100,
            "db_stats": {
                "mine_ore:too_far": {"count": 5, "avg_ms": 3000},
                "mine_ore:success": {"count": 5, "avg_ms": 5000},
            },
        }
        problems = detect_problems(data)
        names = [p["name"] for p in problems]
        assert "db_procedure_failing" not in names

    def test_low_sample_ignored(self):
        """Procedure with 100% fail but <5 attempts → no problem."""
        data = {
            "counts": {},
            "recent_lines": 100,
            "db_stats": {
                "sell_to_vendor:vendor_refused": {"count": 3, "avg_ms": 2000},
            },
        }
        problems = detect_problems(data)
        names = [p["name"] for p in problems]
        assert "db_procedure_failing" not in names

    def test_critical_at_zero_success_high_count(self):
        """0% success with >20 attempts → CRITICAL."""
        data = {
            "counts": {},
            "recent_lines": 100,
            "db_stats": {
                "craft_blacksmith:missing_resource": {"count": 30, "avg_ms": 8000},
            },
        }
        problems = detect_problems(data)
        match = [p for p in problems if p["name"] == "db_procedure_failing"]
        assert len(match) == 1
        assert match[0]["severity"] == "CRITICAL"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/test_self_improve.py -v`
Expected: FAIL — `db_procedure_failing` not found in problem names.

- [ ] **Step 3: Add DB-based detection to `detect_problems()`**

In `tools/self_improve.py`, add this block at the end of `detect_problems()`, just before the `return problems` line (around line 427):

```python
    # --- DB-based failure detection ---
    # db_stats format: {"procedure:result": {"count": N, "avg_ms": M}}
    db_stats = data.get("db_stats", {})
    if db_stats:
        # Aggregate by procedure
        proc_agg: dict[str, dict[str, int]] = {}
        for key, info in db_stats.items():
            proc, result = key.split(":", 1)
            if proc not in proc_agg:
                proc_agg[proc] = {"success": 0, "fail": 0}
            if result == "success":
                proc_agg[proc]["success"] += info["count"]
            else:
                proc_agg[proc]["fail"] += info["count"]

        for proc, agg in proc_agg.items():
            total = agg["success"] + agg["fail"]
            if total < 5:
                continue
            fail_rate = agg["fail"] / total
            if fail_rate > 0.8:
                severity = "CRITICAL" if agg["success"] == 0 and total > 20 else "HIGH"
                problems.append({
                    "severity": severity,
                    "name": "db_procedure_failing",
                    "description": (
                        f"{proc} failing {fail_rate:.0%} "
                        f"({agg['fail']}/{total} attempts)"
                    ),
                    "fix_type": "procedure",
                })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/test_self_improve.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Run full test suite**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add tests/test_self_improve.py tools/self_improve.py
git commit -m "Add DB-based failure detection to self-improvement problem detector"
```

---

### Task 2: Fix Failure Streak Detection in `diagnose.py`

**Files:**
- Modify: `tools/diagnose.py:225-258` (`_detect_failure_streaks()`)
- Modify: `tools/diagnose.py:38-44,100-110,284,309,337` (prompt size limits)

Two bugs in `_detect_failure_streaks()`:
1. `LIMIT 100` may miss data — time filter is sufficient
2. `ORDER BY timestamp DESC` processes newest first, but the loop logic counts consecutive failures assuming chronological order — a success breaks the streak, but since we process newest-first, the first success we encounter is the most recent one, not the oldest one. This means we correctly detect the current streak length. **However**, the actual bug is: with LIMIT 100, if there are >100 entries, we miss early failures and may undercount.

- [ ] **Step 1: Fix streak detection query**

In `tools/diagnose.py`, replace `_detect_failure_streaks()` (lines 225-258):

```python
def _detect_failure_streaks(minutes: int) -> list[dict]:
    """Find procedures that are failing consecutively (stuck loops)."""
    if not DB_FILE.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_FILE))
        cutoff = time.time() - minutes * 60
        # No LIMIT — time filter is sufficient. DESC to count from newest.
        rows = conn.execute("""
            SELECT procedure, result, message
            FROM action_logs WHERE timestamp > ?
            ORDER BY timestamp DESC
        """, (cutoff,)).fetchall()
        conn.close()

        # Count consecutive failures from most recent per procedure
        streaks: dict[str, dict] = {}
        for proc, result, msg in rows:
            if proc not in streaks:
                streaks[proc] = {"procedure": proc, "count": 0,
                                 "messages": [], "top_reason": "", "active": True}
            s = streaks[proc]
            if not s["active"]:
                continue
            if result != "success":
                s["count"] += 1
                if msg and msg not in s["messages"] and len(s["messages"]) < 5:
                    s["messages"].append(msg[:150])
                s["top_reason"] = result
            else:
                s["active"] = False  # streak broken by success

        return [s for s in streaks.values() if s["count"] >= 3]
    except Exception:
        return []
```

- [ ] **Step 2: Reduce prompt data limits**

In `tools/diagnose.py`, make these changes:

Line 38 — `_query_recent_failures` limit: change the call in `collect()`:
```python
        "recent_failures": _query_recent_failures(minutes, limit=15),
```

Line 284 — `_extract_log_lines` return limit:
```python
        return important[-15:]  # last 15 important lines
```

Line 309 — `_extract_server_messages` return limit:
```python
        return messages[-15:]
```

Line 337 — `_extract_planner_logs` return limit:
```python
        return entries[-15:]
```

Line 96 — `render_prompt` recent failures display: change `failures[:20]` to `failures[:10]`:
```python
        for f in failures[:10]:
```

Line 110 — server messages display: change `[:15]` (already 15, keep as-is).

Line 118 — planner decisions display: change `planner[-20:]` to `planner[-10:]`:
```python
        for p in planner[-10:]:
```

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add tools/diagnose.py
git commit -m "Fix failure streak detection: remove LIMIT, reduce prompt size"
```

---

### Task 3: Progressive Timeout + Persist fix_attempts in `supervisor.py`

**Files:**
- Create: `tests/test_supervisor.py`
- Modify: `tools/supervisor.py:365-392` (`call_claude_with_prompt()`)
- Modify: `tools/supervisor.py:449-474` (main loop — fix_attempts init)
- Modify: `tools/supervisor.py:524-548` (targeted fix retry logic)

- [ ] **Step 1: Write failing tests**

Create `tests/test_supervisor.py`:

```python
"""Tests for supervisor — fix attempt persistence and progressive timeout."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_improvements(tmp_path):
    """Create a temp improvements.jsonl file."""
    log_file = tmp_path / "improvements.jsonl"
    return log_file


class TestLoadFixAttempts:
    def test_counts_failed_targeted_fixes(self, tmp_improvements):
        from tools.supervisor import _load_fix_attempts
        entries = [
            {"action": "targeted_fix:craft_blacksmith", "reason": "missing_resource (fail rate 100%)", "success": False, "code_changed": False},
            {"action": "targeted_fix:craft_blacksmith", "reason": "missing_resource (fail rate 100%)", "success": False, "code_changed": False},
            {"action": "targeted_fix:mine_ore", "reason": "too_far (fail rate 90%)", "success": False, "code_changed": False},
            {"action": "auto_recover", "reason": "stuck", "success": True, "code_changed": False},
        ]
        with open(tmp_improvements, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        with patch("tools.supervisor.IMPROVEMENTS_LOG", tmp_improvements):
            attempts = _load_fix_attempts()

        assert attempts["craft_blacksmith:missing_resource"] == 2
        assert attempts["mine_ore:too_far"] == 1
        assert "auto_recover" not in str(attempts)

    def test_resets_on_success(self, tmp_improvements):
        from tools.supervisor import _load_fix_attempts
        entries = [
            {"action": "targeted_fix:craft_blacksmith", "reason": "missing_resource (fail rate 100%)", "success": False, "code_changed": False},
            {"action": "targeted_fix:craft_blacksmith", "reason": "missing_resource (fail rate 100%)", "success": True, "code_changed": True},
            {"action": "targeted_fix:craft_blacksmith", "reason": "missing_resource (fail rate 100%)", "success": False, "code_changed": False},
        ]
        with open(tmp_improvements, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        with patch("tools.supervisor.IMPROVEMENTS_LOG", tmp_improvements):
            attempts = _load_fix_attempts()

        # After success reset, only 1 subsequent failure counted
        assert attempts["craft_blacksmith:missing_resource"] == 1

    def test_empty_file(self, tmp_improvements):
        from tools.supervisor import _load_fix_attempts
        tmp_improvements.write_text("")
        with patch("tools.supervisor.IMPROVEMENTS_LOG", tmp_improvements):
            attempts = _load_fix_attempts()
        assert attempts == {}

    def test_missing_file(self, tmp_path):
        from tools.supervisor import _load_fix_attempts
        missing = tmp_path / "nonexistent.jsonl"
        with patch("tools.supervisor.IMPROVEMENTS_LOG", missing):
            attempts = _load_fix_attempts()
        assert attempts == {}


class TestProgressiveTimeout:
    def test_timeout_increases(self):
        from tools.supervisor import _get_timeout
        assert _get_timeout(0) == 300
        assert _get_timeout(1) == 450
        assert _get_timeout(2) == 600


class TestWriteSupervisorHints:
    def test_writes_skip(self, tmp_path):
        from tools.supervisor import _write_skip_hint
        hints_file = tmp_path / "supervisor_hints.json"
        with patch("tools.supervisor.HINTS_FILE", hints_file):
            _write_skip_hint("craft_blacksmith", "missing_resource", ttl_hours=1)

        data = json.loads(hints_file.read_text())
        assert "craft_blacksmith" in data["skip_procedures"]
        hint = data["skip_procedures"]["craft_blacksmith"]
        assert hint["reason"] == "missing_resource"
        assert "until" in hint

    def test_appends_to_existing(self, tmp_path):
        from tools.supervisor import _write_skip_hint
        hints_file = tmp_path / "supervisor_hints.json"
        hints_file.write_text(json.dumps({
            "skip_procedures": {
                "mine_ore": {"until": 9999999999, "reason": "too_far"}
            }
        }))
        with patch("tools.supervisor.HINTS_FILE", hints_file):
            _write_skip_hint("craft_blacksmith", "missing_resource", ttl_hours=1)

        data = json.loads(hints_file.read_text())
        assert "mine_ore" in data["skip_procedures"]
        assert "craft_blacksmith" in data["skip_procedures"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/test_supervisor.py -v`
Expected: FAIL — `_load_fix_attempts`, `_get_timeout`, `_write_skip_hint` don't exist yet.

- [ ] **Step 3: Add `_load_fix_attempts()` function**

In `tools/supervisor.py`, add after the `IMPROVEMENTS_LOG` constant (line 41), a new constant and function:

```python
HINTS_FILE = ROOT / "data" / "supervisor_hints.json"
```

Then add after `_log_improvement()` (around line 443):

```python
def _load_fix_attempts() -> dict[str, int]:
    """Load fix attempt counts from improvements.jsonl.

    Counts consecutive failed targeted_fix entries per fix_key.
    Resets count when a successful targeted_fix is found for the same key.
    """
    if not IMPROVEMENTS_LOG.exists():
        return {}
    attempts: dict[str, int] = {}
    try:
        with open(IMPROVEMENTS_LOG) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                action = entry.get("action", "")
                if not action.startswith("targeted_fix:"):
                    continue
                # Extract fix_key: "procedure:reason"
                proc = action.split(":", 1)[1]  # e.g. "craft_blacksmith"
                reason_raw = entry.get("reason", "")
                # reason format: "missing_resource (fail rate 100%)"
                reason = reason_raw.split(" (")[0] if " (" in reason_raw else reason_raw
                fix_key = f"{proc}:{reason}"

                if entry.get("success") or entry.get("code_changed"):
                    attempts[fix_key] = 0
                else:
                    attempts[fix_key] = attempts.get(fix_key, 0) + 1
    except Exception:
        pass
    return {k: v for k, v in attempts.items() if v > 0}


def _get_timeout(attempt: int) -> int:
    """Progressive timeout: 300s → 450s → 600s."""
    timeouts = [300, 450, 600]
    return timeouts[min(attempt, len(timeouts) - 1)]


def _write_skip_hint(procedure: str, reason: str, ttl_hours: float = 1.0) -> None:
    """Write a skip hint for the planner to read."""
    import time as _time
    hints: dict = {}
    if HINTS_FILE.exists():
        try:
            hints = json.loads(HINTS_FILE.read_text())
        except Exception:
            pass
    skip = hints.setdefault("skip_procedures", {})
    skip[procedure] = {
        "until": _time.time() + ttl_hours * 3600,
        "reason": reason,
    }
    HINTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HINTS_FILE.write_text(json.dumps(hints, indent=2))
```

- [ ] **Step 4: Update `call_claude_with_prompt()` to accept timeout**

In `tools/supervisor.py`, change the function signature and body (lines 365-392):

```python
def call_claude_with_prompt(prompt: str, timeout: int = 300) -> tuple[bool, str]:
    """Call Claude Code CLI with a prompt. Returns (success, output)."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt,
             "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        output = result.stdout or ""
        if result.stderr:
            output += "\n--- stderr ---\n" + result.stderr

        # Log to claude_code.log
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(CLAUDE_LOG, "a") as f:
            f.write(f"\n{'='*72}\n[{ts}]\n{'='*72}\n")
            f.write(output[:2000])
            f.write("\n")

        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"Claude Code timed out after {timeout}s"
    except FileNotFoundError:
        return False, "Claude Code CLI not found"
```

- [ ] **Step 5: Update main loop to use persistent fix_attempts + progressive timeout + hints**

In `tools/supervisor.py` `main()`, replace the `fix_attempts` initialization (lines 471-473):

```python
    # Load fix attempts from disk (persists across restarts)
    fix_attempts: dict[str, int] = _load_fix_attempts()
    MAX_FIX_ATTEMPTS = 3
```

Then replace the Level 2 targeted fix block (lines 524-548):

```python
                # Level 2: Check DB for specific procedure failures
                failures = get_top_failures(minutes=args.minutes)
                if failures:
                    worst = failures[0]
                    fix_key = f"{worst['procedure']}:{worst['top_failure']}"
                    attempts = fix_attempts.get(fix_key, 0)

                    if worst["fail_rate"] > 0.8 and worst["total"] > 10:
                        if attempts >= MAX_FIX_ATTEMPTS:
                            print(f"[supervisor] Skipping {fix_key} — already failed {attempts} fix attempts")
                            _log_improvement(
                                f"skip:{worst['procedure']}",
                                f"gave up after {attempts} failed fix attempts for {worst['top_failure']}",
                                success=False, code_changed=False,
                            )
                            # Signal planner to skip this procedure
                            _write_skip_hint(
                                worst["procedure"],
                                worst["top_failure"],
                                ttl_hours=1.0,
                            )
                        else:
                            stop_agent(agent_proc)
                            timeout = _get_timeout(attempts)
                            # Patch call with progressive timeout
                            prompt = build_diagnostic_prompt(failure=worst)
                            proc_name = worst["procedure"]
                            ts_str = datetime.now().strftime("%H:%M:%S")
                            print(f"[supervisor] [{ts_str}] TARGETED FIX: {proc_name} ({worst['top_failure']}) [timeout={timeout}s, attempt {attempts+1}/{MAX_FIX_ATTEMPTS}]")

                            head_before = get_git_head()
                            success, output = call_claude_with_prompt(prompt, timeout=timeout)
                            head_after = get_git_head()

                            code_changed = head_before != head_after and head_after != ""
                            _log_improvement(
                                f"targeted_fix:{proc_name}",
                                f"{worst['top_failure']} (fail rate {worst['fail_rate']:.0%})",
                                success=success,
                                code_changed=code_changed,
                                output_preview=output[:500],
                            )
                            fix_attempts[fix_key] = attempts + 1
                            if code_changed:
                                fix_attempts[fix_key] = 0  # reset on success
                                print(f"[supervisor] Targeted fix committed for {proc_name}")
                            elif success:
                                print(f"[supervisor] Claude ran but no changes for {proc_name}")
                            else:
                                print(f"[supervisor] Claude failed for {proc_name}")

                            agent_proc = start_agent(args.agent_args)
                            last_analysis = time.time()
                            continue
```

Also update the Level 3 full analysis `call_claude_with_prompt` call (around line 556-558) to use timeout parameter:

```python
                        _, output = call_claude_with_prompt(prompt, timeout=600)
```

- [ ] **Step 6: Remove the now-inlined `run_targeted_fix()` function**

Delete the `run_targeted_fix()` function (lines 335-362) since its logic is now inlined in the main loop with progressive timeout support.

- [ ] **Step 7: Run tests**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/test_supervisor.py -v`
Expected: All tests PASS.

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest`
Expected: All tests pass.

- [ ] **Step 8: Commit**

```bash
git add tools/supervisor.py tests/test_supervisor.py
git commit -m "Supervisor: progressive timeout, persist fix attempts, write skip hints"
```

---

### Task 4: Planner Reads Supervisor Hints

**Files:**
- Modify: `anima/planner/planner.py:145-156` (`tick()` method)
- Modify: `tests/test_planner.py` (add hint tests)

- [ ] **Step 1: Write failing test**

Add to `tests/test_planner.py`:

```python
import json
import time as _time
from unittest.mock import patch


class TestSupervisorHints:
    @pytest.mark.asyncio
    async def test_skips_hinted_procedure(self, tmp_path):
        """Planner skips procedure listed in supervisor_hints.json."""
        hints_file = tmp_path / "supervisor_hints.json"
        hints_file.write_text(json.dumps({
            "skip_procedures": {
                "craft_blacksmith": {
                    "until": _time.time() + 3600,
                    "reason": "missing_resource",
                }
            }
        }))

        reg = ProcedureRegistry()
        reg.register(StubProcedure("craft_blacksmith"))
        reg.register(StubProcedure("mine_ore"))
        planner = Planner(reg)

        ctx = _make_ctx()
        _add_item(ctx, 1, PICKAXE)
        _add_item(ctx, 2, INGOT, amount=20)

        with patch("anima.planner.planner.SUPERVISOR_HINTS_FILE", hints_file):
            proc = await planner.select_procedure(ctx)

        # Should skip craft_blacksmith and pick mine_ore or something else
        assert proc is None or proc.name != "craft_blacksmith"

    @pytest.mark.asyncio
    async def test_expired_hint_ignored(self, tmp_path):
        """Expired hint does not skip procedure."""
        hints_file = tmp_path / "supervisor_hints.json"
        hints_file.write_text(json.dumps({
            "skip_procedures": {
                "craft_blacksmith": {
                    "until": _time.time() - 100,  # expired
                    "reason": "missing_resource",
                }
            }
        }))

        reg = ProcedureRegistry()
        reg.register(StubProcedure("craft_blacksmith"))
        planner = Planner(reg)

        ctx = _make_ctx()
        _add_item(ctx, 1, PICKAXE)
        _add_item(ctx, 2, INGOT, amount=20)

        with patch("anima.planner.planner.SUPERVISOR_HINTS_FILE", hints_file):
            # craft_blacksmith should NOT be skipped
            result = await planner.tick(ctx)
            # tick returns result from running the procedure (StubProcedure always succeeds)
            assert result is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/test_planner.py::TestSupervisorHints -v`
Expected: FAIL — `SUPERVISOR_HINTS_FILE` not defined.

- [ ] **Step 3: Add hints reading to planner**

In `anima/planner/planner.py`, add near the top imports (after line 12):

```python
import json
import time as _time
from pathlib import Path

SUPERVISOR_HINTS_FILE = Path(__file__).parent.parent.parent / "data" / "supervisor_hints.json"
```

Then in the `tick()` method, add the supervisor hint check right after the existing `_skip_procedures` check (after line 155). Replace lines 145-156 with:

```python
    async def tick(self, ctx: AgentContext) -> ProcedureResult | None:
        """One planner cycle: select procedure → run it → return result."""
        proc = await self.select_procedure(ctx)
        if proc is None:
            return None

        # Check if this procedure was marked for skipping (repeat failure)
        skip = ctx.blackboard.get("_skip_procedures", set())
        if proc.name in skip:
            logger.info("planner_skipping", procedure=proc.name, reason="repeat failure")
            return None

        # Check supervisor hints (supervisor gave up fixing this procedure)
        if _is_supervisor_skipped(proc.name):
            logger.info("planner_skipping", procedure=proc.name, reason="supervisor hint")
            return None

        logger.info("planner_selected", procedure=proc.name)
```

Add the helper function before the `Planner` class:

```python
def _is_supervisor_skipped(procedure: str) -> bool:
    """Check if supervisor has flagged this procedure for skipping."""
    if not SUPERVISOR_HINTS_FILE.exists():
        return False
    try:
        hints = json.loads(SUPERVISOR_HINTS_FILE.read_text())
        skip = hints.get("skip_procedures", {})
        entry = skip.get(procedure)
        if entry and entry.get("until", 0) > _time.time():
            return True
    except Exception:
        pass
    return False
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest tests/test_planner.py -v`
Expected: All tests PASS (including new `TestSupervisorHints`).

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add anima/planner/planner.py tests/test_planner.py
git commit -m "Planner reads supervisor hints to skip broken procedures"
```

---

### Task 5: Integration Verification

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `cd /Users/dkkang/dev/uo/anima && uv run pytest -v`
Expected: All tests pass.

- [ ] **Step 2: Run linter**

Run: `cd /Users/dkkang/dev/uo/anima && uv run ruff check`
Expected: No errors.

- [ ] **Step 3: Run formatter**

Run: `cd /Users/dkkang/dev/uo/anima && uv run ruff format --check`
Expected: No formatting issues, or fix with `uv run ruff format`.

- [ ] **Step 4: Manual smoke test of diagnose.py**

Run: `cd /Users/dkkang/dev/uo/anima && uv run python tools/diagnose.py --minutes 30`
Expected: Output shows diagnostic data with reduced prompt size. Failure streaks detected if DB has failures.

- [ ] **Step 5: Manual smoke test of self_improve.py**

Run: `cd /Users/dkkang/dev/uo/anima && uv run python tools/self_improve.py --minutes 30`
Expected: If DB has high-failure procedures, report should list `db_procedure_failing` as a problem instead of "No problems detected."
