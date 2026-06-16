"""Robustness guard: a hung claude mutation must be killed group-wide on
timeout, not leave a grandchild holding the stdout pipe (which wedged a slot
for 3h during a network outage).

Reproduces the exact failure: a child that backgrounds a long sleep inheriting
stdout. proc.kill() alone kills only the leader → the sleep keeps the pipe open
→ communicate() blocks forever. _kill_process_group (os.killpg) kills the whole
group so the pipe closes and the final drain returns promptly.
"""
import subprocess
import time

import pytest

from foundry.mutate import _kill_process_group


def test_kill_process_group_unblocks_inherited_pipe():
    # sh backgrounds a 30s sleep (inherits stdout) then waits on it → the pipe
    # stays open while the sleep lives.
    proc = subprocess.Popen(
        ["sh", "-c", "sleep 30 & echo started; wait"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    with pytest.raises(subprocess.TimeoutExpired):
        proc.communicate(timeout=1)

    _kill_process_group(proc)

    t0 = time.monotonic()
    proc.communicate(timeout=10)            # must NOT hang now
    assert time.monotonic() - t0 < 9
    assert proc.poll() is not None          # process group is dead


def test_kill_process_group_safe_on_dead_process():
    proc = subprocess.Popen(["true"], start_new_session=True)
    proc.wait()
    # already exited — must not raise
    _kill_process_group(proc)
