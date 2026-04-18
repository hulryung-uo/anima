# Supervisor Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the self-improvement loop from misreporting restart-only recoveries as successes, and make the Claude-Code fix subprocess incapable of hanging past its timeout.

**Architecture:** Touch only `tools/supervisor.py` and `tools/fix_lock.py` plus their tests. Four small, independent changes: (1) honest outcome reporting with degradation detection, (2) PID liveness check and shorter TTL in fix lock, (3) wall-clock watchdog around `claude -p` subprocess, (4) startup sweep for stale locks.

**Tech Stack:** Python 3.12 stdlib (`os`, `signal`, `subprocess`, `threading`), pytest, monkeypatch for isolation.

**Context:** Yesterday the agent hit a starvation spiral — gold=4, empty inventory — while a stuck `claude -p` (PID 8324) held `data/fixing.json` for >2h even though `MAX_LOCK_AGE=1800`. Ten+ auto-recoveries fired but every one logged `success=true, code_changed=false`, so the improvement log looks like a wall of green. This plan fixes the telemetry first (so we can *see* regressions), then the subprocess robustness (so the lock actually frees).

---

## File Structure

- `tools/supervisor.py` — add `outcome` field to improvement log, degradation detector, wall-clock watchdog, startup lock sweep.
- `tools/fix_lock.py` — add PID liveness check; lower `MAX_LOCK_AGE` from 1800s to 1200s; add `sweep_stale_lock()`.
- `tests/test_supervisor.py` — new tests for outcome field, degradation, watchdog.
- `tests/test_fix_lock.py` — new tests for PID liveness and sweep.

---

### Task 1: Record honest outcomes in the improvement log

**Why:** Today every auto-recover logs `success=true, code_changed=false`, so 16+ consecutive no-op restarts look successful. We need a distinct `outcome` field: `"restart_only"`, `"code_fix"`, `"skipped"`, `"failed"`.

**Files:**
- Modify: `tools/supervisor.py:580-593` (`_log_improvement`)
- Modify: `tools/supervisor.py:349` (auto_recover call), `:884-890` (targeted_fix call), `:927-929` (full_analysis call), `:841-845` (skip call)
- Test: `tests/test_supervisor.py`

- [ ] **Step 1: Write failing test for outcome field**

Append to `tests/test_supervisor.py`:

```python
class TestLogImprovementOutcome:
    def test_auto_recover_writes_restart_only_outcome(self, tmp_path, monkeypatch):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        monkeypatch.setattr(supervisor, "get_git_head", lambda: "abc1234")

        supervisor._log_improvement(
            "auto_recover", "stuck loop", success=True, code_changed=False,
        )
        entry = json.loads(log.read_text().strip())
        assert entry["outcome"] == "restart_only"
        assert entry["code_changed"] is False

    def test_targeted_fix_with_commit_writes_code_fix_outcome(
        self, tmp_path, monkeypatch,
    ):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        monkeypatch.setattr(supervisor, "get_git_head", lambda: "abc1234")

        supervisor._log_improvement(
            "targeted_fix:mine_ore", "bad", success=True, code_changed=True,
        )
        entry = json.loads(log.read_text().strip())
        assert entry["outcome"] == "code_fix"

    def test_failed_claude_writes_failed_outcome(self, tmp_path, monkeypatch):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        monkeypatch.setattr(supervisor, "get_git_head", lambda: "abc1234")

        supervisor._log_improvement(
            "targeted_fix:x", "bad", success=False, code_changed=False,
        )
        entry = json.loads(log.read_text().strip())
        assert entry["outcome"] == "failed"

    def test_skip_writes_skipped_outcome(self, tmp_path, monkeypatch):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        monkeypatch.setattr(supervisor, "get_git_head", lambda: "abc1234")

        supervisor._log_improvement(
            "skip:craft_blacksmith", "give up", success=False, code_changed=False,
        )
        entry = json.loads(log.read_text().strip())
        assert entry["outcome"] == "skipped"
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/test_supervisor.py::TestLogImprovementOutcome -x`
Expected: FAIL — `outcome` key missing.

- [ ] **Step 3: Implement — add `outcome` derivation**

Edit `tools/supervisor.py:580-593` (the `_log_improvement` function):

```python
def _derive_outcome(action: str, success: bool, code_changed: bool) -> str:
    """Honest outcome label, distinct from the back-compat `success` field.

    - code_fix   : Claude committed a fix
    - restart_only : auto_recover that only restarted the agent
    - skipped    : supervisor gave up on a fix_key
    - failed     : Claude ran but failed, or timeout hit
    """
    if code_changed:
        return "code_fix"
    if action.startswith("skip:"):
        return "skipped"
    if action == "auto_recover":
        return "restart_only"
    return "failed"


def _log_improvement(action: str, reason: str, success: bool,
                     code_changed: bool, output_preview: str = "") -> None:
    IMPROVEMENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "reason": reason,
        "success": success,  # kept for backward-compat readers
        "outcome": _derive_outcome(action, success, code_changed),
        "code_changed": code_changed,
        "commit": get_git_head()[:8],
        "output_preview": output_preview[:300],
    }
    with open(IMPROVEMENTS_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
```

- [ ] **Step 4: Run — expect pass**

Run: `uv run pytest tests/test_supervisor.py::TestLogImprovementOutcome -x`
Expected: PASS (4 tests).

- [ ] **Step 5: Run full supervisor test suite for regressions**

Run: `uv run pytest tests/test_supervisor.py tests/test_fix_lock.py -x`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tools/supervisor.py tests/test_supervisor.py
git commit -m "Add honest outcome field to supervisor improvement log"
```

---

### Task 2: Degradation alert when restarts don't fix anything

**Why:** If 5+ consecutive improvement entries are restart-only or failed with no `code_fix`, the self-improvement loop is spinning without progress. Alert operator via `[supervisor]` stdio and a marker file so a human notices.

**Files:**
- Modify: `tools/supervisor.py` — add `_count_unproductive_streak()`, `_check_degradation()`; call from main loop.
- Test: `tests/test_supervisor.py`

- [ ] **Step 1: Write failing test for streak counter**

Append to `tests/test_supervisor.py`:

```python
class TestDegradationDetection:
    def _entry(self, outcome: str) -> dict:
        return {
            "ts": "2026-04-18 00:00:00", "action": "auto_recover",
            "reason": "x", "success": True, "outcome": outcome,
            "code_changed": outcome == "code_fix", "commit": "abc",
            "output_preview": "",
        }

    def test_streak_counts_restart_only_entries(self, tmp_path, monkeypatch):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        with open(log, "w") as f:
            for _ in range(4):
                f.write(json.dumps(self._entry("restart_only")) + "\n")
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        assert supervisor._count_unproductive_streak() == 4

    def test_streak_resets_on_code_fix(self, tmp_path, monkeypatch):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        with open(log, "w") as f:
            for _ in range(3):
                f.write(json.dumps(self._entry("restart_only")) + "\n")
            f.write(json.dumps(self._entry("code_fix")) + "\n")
            f.write(json.dumps(self._entry("restart_only")) + "\n")
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        assert supervisor._count_unproductive_streak() == 1

    def test_degradation_threshold_triggers_alert_file(self, tmp_path, monkeypatch):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        flag = tmp_path / "supervisor_alert.flag"
        with open(log, "w") as f:
            for _ in range(5):
                f.write(json.dumps(self._entry("restart_only")) + "\n")
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        monkeypatch.setattr(supervisor, "ALERT_FLAG", flag)

        triggered = supervisor._check_degradation()
        assert triggered is True
        assert flag.exists()
        data = json.loads(flag.read_text())
        assert data["streak"] == 5

    def test_no_alert_below_threshold(self, tmp_path, monkeypatch):
        from tools import supervisor
        log = tmp_path / "improvements.jsonl"
        flag = tmp_path / "supervisor_alert.flag"
        with open(log, "w") as f:
            for _ in range(4):
                f.write(json.dumps(self._entry("restart_only")) + "\n")
        monkeypatch.setattr(supervisor, "IMPROVEMENTS_LOG", log)
        monkeypatch.setattr(supervisor, "ALERT_FLAG", flag)

        assert supervisor._check_degradation() is False
        assert not flag.exists()
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/test_supervisor.py::TestDegradationDetection -x`
Expected: FAIL — `_count_unproductive_streak` / `_check_degradation` / `ALERT_FLAG` undefined.

- [ ] **Step 3: Implement degradation detection**

Add near the other constants in `tools/supervisor.py` (around line 52, next to `HINTS_FILE`):

```python
ALERT_FLAG = ROOT / "data" / "supervisor_alert.flag"
UNPRODUCTIVE_STREAK_THRESHOLD = 5
```

Add these functions after `_load_fix_attempts` (around line 628, before `_get_timeout`):

```python
def _count_unproductive_streak() -> int:
    """Return the count of trailing entries with outcome != 'code_fix'.

    Reads improvements.jsonl newest-first and stops at the first code_fix.
    Unknown entries (no outcome field, older logs) are treated as 'failed'
    so legacy data doesn't mask current degradation.
    """
    if not IMPROVEMENTS_LOG.exists():
        return 0
    streak = 0
    try:
        lines = IMPROVEMENTS_LOG.read_text().splitlines()
    except Exception:
        return 0
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        outcome = entry.get("outcome", "failed")
        if outcome == "code_fix":
            break
        streak += 1
    return streak


def _check_degradation() -> bool:
    """Emit an alert if the unproductive streak crosses the threshold.

    Writes data/supervisor_alert.flag and logs to stdio. Safe to call
    repeatedly — overwrites the flag with the latest streak count.
    Returns True if an alert was emitted this call.
    """
    streak = _count_unproductive_streak()
    if streak < UNPRODUCTIVE_STREAK_THRESHOLD:
        return False
    try:
        ALERT_FLAG.parent.mkdir(parents=True, exist_ok=True)
        ALERT_FLAG.write_text(json.dumps({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "streak": streak,
            "threshold": UNPRODUCTIVE_STREAK_THRESHOLD,
            "message": "Self-improvement loop not producing code fixes",
        }, indent=2))
    except Exception:
        pass
    _alert(
        f"⚠ DEGRADATION: {streak} consecutive recoveries without a code fix "
        f"(flag → {ALERT_FLAG})"
    )
    return True
```

- [ ] **Step 4: Run — expect pass**

Run: `uv run pytest tests/test_supervisor.py::TestDegradationDetection -x`
Expected: PASS (4 tests).

- [ ] **Step 5: Wire into main loop**

In `tools/supervisor.py` inside `main()`, right after the `auto_recover` call at line 813 (`agent_proc = auto_recover(...)`), add one line:

```python
                        agent_proc = auto_recover(agent_proc, problem, args.agent_args)
                        last_analysis = now
                        _check_degradation()
                        continue
```

Also add the same call after the `targeted_fix` and `full_analysis` branches — anywhere `_log_improvement` is called with `code_changed` possibly False. Simplest: add `_check_degradation()` at the end of each code path that just logged an improvement. Three sites:

Replace the end of the targeted_fix branch (around line 899-902):
```python
                            else:
                                _debug(f"Claude failed for {proc_name}")
                                fix_attempts[fix_key] = attempts + 1
                            _check_degradation()
                            agent_proc = start_agent(args.agent_args)
                            last_analysis = time.time()
                            continue
```

Replace the end of the full_analysis branch (around line 929-931):
```python
                        _log_improvement("full_analysis", severe[0]["name"],
                                         success=True, code_changed=code_changed,
                                         output_preview=output[:500])
                        _check_degradation()
                        agent_proc = start_agent(args.agent_args)
                        last_analysis = time.time()
```

- [ ] **Step 6: Re-run full suite**

Run: `uv run pytest tests/test_supervisor.py -x`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add tools/supervisor.py tests/test_supervisor.py
git commit -m "Alert when self-improvement streak has no code fixes"
```

---

### Task 3: PID liveness check in fix lock

**Why:** Yesterday a `claude -p` subprocess outlived its parent's subprocess.run timeout (possibly orphaned after parent restart). Its lock lived in `data/fixing.json` for >2h. If the recorded PID is not alive, the lock should be ignored regardless of age.

**Files:**
- Modify: `tools/fix_lock.py` — add `_pid_alive()`, use in `is_fix_locked` / `acquire_fix_lock`; reduce `MAX_LOCK_AGE` to 1200s.
- Test: `tests/test_fix_lock.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_fix_lock.py`:

```python
class TestPidLiveness:
    def test_lock_ignored_when_pid_dead(self, tmp_lock, monkeypatch):
        """A lock owned by a dead PID is treated as stale even if fresh."""
        from tools.fix_lock import acquire_fix_lock, is_fix_locked
        # Dead PID, fresh timestamp
        tmp_lock.write_text(json.dumps({
            "problem": "X", "pid": 999999, "sha": "abc",
            "started": time.time(),
        }))
        monkeypatch.setattr("tools.fix_lock._pid_alive", lambda p: False)
        assert is_fix_locked("X") is False
        assert acquire_fix_lock("X", pid=1, sha="abc") is True

    def test_lock_honored_when_pid_alive(self, tmp_lock, monkeypatch):
        from tools.fix_lock import acquire_fix_lock, is_fix_locked
        tmp_lock.write_text(json.dumps({
            "problem": "X", "pid": 42, "sha": "abc",
            "started": time.time(),
        }))
        monkeypatch.setattr("tools.fix_lock._pid_alive", lambda p: True)
        assert is_fix_locked("X") is True
        assert acquire_fix_lock("X", pid=1, sha="abc") is False

    def test_max_lock_age_lowered(self):
        from tools.fix_lock import MAX_LOCK_AGE
        assert MAX_LOCK_AGE <= 1200
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/test_fix_lock.py::TestPidLiveness -x`
Expected: FAIL — `_pid_alive` undefined and `MAX_LOCK_AGE` too high.

- [ ] **Step 3: Implement `_pid_alive` and lower TTL**

Edit `tools/fix_lock.py`. Replace the `MAX_LOCK_AGE` line (line 26) with:

```python
# Lock is considered stale after this many seconds (covers crashed sessions)
MAX_LOCK_AGE = 1200  # 20 minutes
```

Add this helper after `_current_sha` (around line 48):

```python
def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check for a PID on POSIX.

    Uses kill(pid, 0): returns True if the signal could be delivered,
    False if the process doesn't exist. Treats EPERM (different owner)
    as alive to avoid false negatives. On error, returns True so we err
    on the side of honoring the lock.
    """
    if pid <= 0:
        return False
    try:
        import os
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True
```

Update `is_fix_locked` (lines 50-67) to check PID:

```python
def is_fix_locked(problem: str) -> bool:
    """True if another session is actively fixing the given problem.

    Returns False if the lock file is missing, malformed, stale by age,
    or owned by a dead PID.
    """
    data = _read_lock()
    if not data:
        return False
    if data.get("problem") != problem:
        return False
    started = data.get("started", 0)
    if time.time() - started > MAX_LOCK_AGE:
        return False
    pid = data.get("pid", 0)
    if pid and not _pid_alive(pid):
        return False
    return True
```

Update `acquire_fix_lock` (lines 70-99) — add a PID liveness check alongside the SHA check:

```python
def acquire_fix_lock(problem: str, pid: int, sha: str) -> bool:
    """Try to acquire the fix lock for `problem`. Returns False on conflict.

    A lock held for the same problem is treated as stale if any of:
      - age > MAX_LOCK_AGE
      - stored SHA differs from incoming SHA (commit moved on)
      - stored PID is not alive (process crashed/orphaned)
    """
    data = _read_lock()
    if data and data.get("problem") == problem:
        started = data.get("started", 0)
        if time.time() - started <= MAX_LOCK_AGE:
            locked_pid = data.get("pid", 0)
            if locked_pid and not _pid_alive(locked_pid):
                # Process gone — allow re-acquisition.
                pass
            else:
                locked_sha = data.get("sha", "")
                if locked_sha and sha and locked_sha == sha:
                    return False
                elif not locked_sha or not sha:
                    return False
                # SHAs differ → stale due to new commit, fall through.
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({
        "problem": problem,
        "pid": pid,
        "sha": sha,
        "started": time.time(),
    }, indent=2))
    return True
```

- [ ] **Step 4: Run — expect pass**

Run: `uv run pytest tests/test_fix_lock.py -x`
Expected: PASS (all pre-existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add tools/fix_lock.py tests/test_fix_lock.py
git commit -m "Treat fix lock as stale when owning PID is dead"
```

---

### Task 4: Wall-clock watchdog for `claude -p` subprocess

**Why:** `subprocess.run(..., timeout=N)` sends SIGKILL to the direct child, but an unresponsive `claude` can leave grandchildren orphaned, and on some platforms the timeout doesn't fire for blocked I/O reliably. Add a hard wall-clock killer via a background `Timer` that issues SIGKILL to the process group.

**Files:**
- Modify: `tools/supervisor.py:504-544` (`call_claude_with_prompt`) — use `Popen` with `preexec_fn=os.setsid` + background `Timer` that calls `os.killpg(..., SIGKILL)`.
- Test: `tests/test_supervisor.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_supervisor.py`:

```python
class TestClaudeWatchdog:
    def test_wall_clock_timeout_kills_process_group(self, tmp_path, monkeypatch):
        """If claude hangs past timeout+grace, the supervisor kills the pgid."""
        import sys
        from tools import supervisor

        monkeypatch.setattr(supervisor, "CLAUDE_LOG", tmp_path / "claude.log")
        # Replace `claude` with a python subprocess that sleeps forever
        hang_cmd = [sys.executable, "-c", "import time; time.sleep(600)"]
        monkeypatch.setattr(supervisor, "_CLAUDE_CMD", hang_cmd)

        t0 = time.time()
        success, output = supervisor.call_claude_with_prompt("diag", timeout=2)
        elapsed = time.time() - t0

        assert success is False
        # Kill should happen within timeout + generous grace (5s) on CI
        assert elapsed < 10, f"watchdog took too long: {elapsed:.1f}s"
        assert "timed out" in output.lower() or "killed" in output.lower()
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/test_supervisor.py::TestClaudeWatchdog -x`
Expected: FAIL — `_CLAUDE_CMD` not yet a module attribute; current implementation uses a hard-coded `["claude", ...]`.

- [ ] **Step 3: Implement watchdog**

Replace the body of `call_claude_with_prompt` in `tools/supervisor.py` (lines 504-544). Also add `_CLAUDE_CMD` as a module-level default so tests can patch it:

```python
_CLAUDE_CMD: list[str] = ["claude", "-p"]
_WATCHDOG_GRACE_SECONDS = 30  # extra time after subprocess.run timeout before SIGKILL


def call_claude_with_prompt(prompt: str, timeout: int = 300) -> tuple[bool, str]:
    """Call Claude Code CLI with a prompt. Returns (success, output).

    Uses a background watchdog that issues SIGKILL to the whole process
    group if the subprocess is still alive `timeout + _WATCHDOG_GRACE_SECONDS`
    after launch. This covers cases where subprocess.run's timeout fails to
    reap a blocked child or its descendants.
    """
    import threading
    try:
        CLAUDE_LOG.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    cmd = list(_CLAUDE_CMD) + [prompt]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # new pgid so we can killpg
            text=True,
        )
    except FileNotFoundError:
        return False, "Claude Code CLI not found"

    killed = {"by_watchdog": False}

    def _watchdog() -> None:
        if proc.poll() is None:
            killed["by_watchdog"] = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    t = threading.Timer(timeout + _WATCHDOG_GRACE_SECONDS, _watchdog)
    t.daemon = True
    t.start()
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass
            stdout, stderr = proc.communicate()
    finally:
        t.cancel()

    output = (stdout or "")
    if stderr:
        output += "\n--- stderr ---\n" + stderr

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(CLAUDE_LOG, "a") as f:
            suffix = ""
            if timed_out:
                suffix = f" TIMED OUT after {timeout}s"
            if killed["by_watchdog"]:
                suffix += " WATCHDOG_KILLED"
            f.write(f"\n{'='*72}\n[{ts}]{suffix}\n{'='*72}\n")
            f.write((output or "(no output captured)")[:2000])
            f.write("\n")
    except Exception:
        pass

    if timed_out:
        note = f"Claude Code timed out after {timeout}s"
        if killed["by_watchdog"]:
            note += " (watchdog killed process group)"
        return False, f"{note}\n{output[:500]}"
    if killed["by_watchdog"]:
        return False, f"Watchdog killed Claude Code\n{output[:500]}"

    return proc.returncode == 0, output
```

- [ ] **Step 4: Run — expect pass**

Run: `uv run pytest tests/test_supervisor.py::TestClaudeWatchdog -x`
Expected: PASS (within 10s).

- [ ] **Step 5: Run full supervisor/fix_lock suite**

Run: `uv run pytest tests/test_supervisor.py tests/test_fix_lock.py -x`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tools/supervisor.py tests/test_supervisor.py
git commit -m "Wall-clock watchdog kills hung claude subprocess group"
```

---

### Task 5: Startup sweep for stale locks

**Why:** Defence in depth — when the supervisor starts fresh (after a crash or restart), sweep `data/fixing.json` once. If the owning PID is dead or the age is over MAX_LOCK_AGE, delete the file and log it.

**Files:**
- Modify: `tools/fix_lock.py` — add `sweep_stale_lock()`.
- Modify: `tools/supervisor.py:main()` — call sweep once at startup.
- Test: `tests/test_fix_lock.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_fix_lock.py`:

```python
class TestSweepStaleLock:
    def test_sweep_removes_dead_pid_lock(self, tmp_lock, monkeypatch):
        from tools.fix_lock import sweep_stale_lock
        tmp_lock.write_text(json.dumps({
            "problem": "X", "pid": 999999, "sha": "abc",
            "started": time.time(),
        }))
        monkeypatch.setattr("tools.fix_lock._pid_alive", lambda p: False)
        removed = sweep_stale_lock()
        assert removed is True
        assert not tmp_lock.exists()

    def test_sweep_removes_age_stale_lock(self, tmp_lock):
        from tools.fix_lock import sweep_stale_lock, MAX_LOCK_AGE
        tmp_lock.write_text(json.dumps({
            "problem": "X", "pid": 1, "sha": "abc",
            "started": time.time() - MAX_LOCK_AGE - 10,
        }))
        assert sweep_stale_lock() is True
        assert not tmp_lock.exists()

    def test_sweep_keeps_fresh_alive_lock(self, tmp_lock, monkeypatch):
        from tools.fix_lock import sweep_stale_lock
        tmp_lock.write_text(json.dumps({
            "problem": "X", "pid": 42, "sha": "abc",
            "started": time.time(),
        }))
        monkeypatch.setattr("tools.fix_lock._pid_alive", lambda p: True)
        assert sweep_stale_lock() is False
        assert tmp_lock.exists()

    def test_sweep_missing_lock_returns_false(self, tmp_lock):
        from tools.fix_lock import sweep_stale_lock
        # tmp_lock fixture ensures the path is unique; file does not exist
        assert sweep_stale_lock() is False
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run pytest tests/test_fix_lock.py::TestSweepStaleLock -x`
Expected: FAIL — `sweep_stale_lock` undefined.

- [ ] **Step 3: Implement sweep**

Append to `tools/fix_lock.py` (after `release_fix_lock`):

```python
def sweep_stale_lock() -> bool:
    """Remove the lock file if it's stale (age or dead PID).

    Called by the supervisor on startup so that a crashed session's lock
    can't block a freshly-started supervisor. Returns True if a stale
    lock was removed.
    """
    data = _read_lock()
    if not data:
        return False
    started = data.get("started", 0)
    pid = data.get("pid", 0)
    stale_age = time.time() - started > MAX_LOCK_AGE
    stale_pid = bool(pid) and not _pid_alive(pid)
    if stale_age or stale_pid:
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass
        return True
    return False
```

- [ ] **Step 4: Run — expect pass**

Run: `uv run pytest tests/test_fix_lock.py::TestSweepStaleLock -x`
Expected: PASS (4 tests).

- [ ] **Step 5: Wire into supervisor startup**

In `tools/supervisor.py`, at the top of `main()` right after `args = parser.parse_args()` (around line 733), add:

```python
    # Clear any stale lock from a previous crashed session
    try:
        from fix_lock import sweep_stale_lock
        if sweep_stale_lock():
            _alert("Swept stale fix lock at startup")
    except Exception as e:
        _debug(f"Lock sweep error: {e}")
```

- [ ] **Step 6: Run full suite**

Run: `uv run pytest tests/test_supervisor.py tests/test_fix_lock.py -x`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add tools/supervisor.py tools/fix_lock.py tests/test_fix_lock.py
git commit -m "Sweep stale fix lock on supervisor startup"
```

---

### Task 6: Final verification — run full project test suite

- [ ] **Step 1: Full test run**

Run: `uv run pytest tests/ -x --ignore=tests/tools`
Expected: All tests PASS, no regressions.

- [ ] **Step 2: Lint**

Run: `uv run ruff check tools/supervisor.py tools/fix_lock.py`
Expected: No errors.

- [ ] **Step 3: Smoke-import the modules**

Run: `uv run python -c "from tools import supervisor, fix_lock; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Inspect improvements.jsonl after the next auto-recover**

Run: `tail -1 data/improvements.jsonl | python3 -m json.tool`
Expected: Newly-written entries include an `outcome` field. Legacy entries without the field are fine.

---

## Out of scope (future plans)

- **Area B — vendor/pathfinding bugs** (`context_menu_timeout` on sell, `permanent_denied` tile cache without TTL, title parsing for " Autumn the armourer"). Will need a dedicated plan after A stabilizes so regressions are visible.
- **Area C — starvation recovery** (bootstrap procedure when gold≈0 and bank≈0). Depends on A — without reliable telemetry we can't tell whether the new recovery works.
- **Area D — log rotation** (`anima.log` 580MB, `metrics_events.jsonl` 206MB). Small but touches `anima/` logging config; separate plan.
