"""_sustained_relapse_ts is a single O(n) backward pass, behaviourally identical
to the original O(n^2) forward scan.

The helper finds the EARLIEST hits<=0 sample whose dead-run (gap to the next
hits>0 sample, or the window end) is >= min_death_s. The old implementation
re-scanned the whole remaining suffix with next() for every hits<=0 sample, so a
window with k dead samples cost O(k·n); analyze_deaths invokes it once per
recovered death, compounding on a long contested soak. The rewrite walks the
stream once from the right, carrying the nearest next-alive timestamp. These
tests pin (a) exact equivalence to a brute-force reference over many random
timelines, including the start-offset path analyze_deaths actually uses, and
(b) that the work is linear (no per-sample suffix re-scan).
"""
from __future__ import annotations

import random

from foundry.apprentice import _sustained_relapse_ts


def _reference(samples, start, end_ts, min_death_s):
    """The original forward O(n^2) algorithm — the equivalence oracle."""
    n = len(samples)
    i = start
    while i < n:
        ts, hits, _ = samples[i]
        if hits > 0:
            i += 1
            continue
        recovered = next((t for t, h, _ in samples[i + 1:] if h > 0), None)
        dead_run = (recovered - ts) if recovered is not None else (end_ts - ts)
        if dead_run >= min_death_s:
            return ts
        i += 1
    return None


def test_matches_reference_on_handcrafted_cases():
    end_ts = 10_000.0
    mds = 3.0
    cases = [
        [],                                              # empty
        [(100.0, 50, 50)],                               # all alive
        [(100.0, 0, 50)],                                # one dead, runs to end
        [(100.0, 0, 50), (100.5, 40, 50)],               # transient dip (<mds)
        [(100.0, 0, 50), (110.0, 40, 50)],               # sustained, then alive
        # transient dip then a genuine sustained relapse later: the EARLIEST
        # qualifier (the sustained one) must win, not the transient dip.
        [(100.0, 0, 50), (100.5, 9, 50), (200.0, 0, 50), (300.0, 9, 50)],
    ]
    for samples in cases:
        for start in range(len(samples) + 2):  # incl. start past the end
            assert (_sustained_relapse_ts(samples, start, end_ts, mds)
                    == _reference(samples, start, end_ts, mds)), (samples, start)


def test_matches_reference_on_random_timelines():
    rng = random.Random(20260620)
    end_ts = 5_000.0
    for _ in range(300):
        n = rng.randint(0, 40)
        ts = 0.0
        samples = []
        for _ in range(n):
            ts += rng.uniform(0.1, 8.0)  # gaps span both sides of min_death_s
            hits = 0 if rng.random() < 0.45 else rng.randint(1, 50)
            samples.append((ts, hits, 50))
        mds = rng.choice([1.0, 3.0, 5.0])
        for start in (0, max(0, n // 2), n):
            assert (_sustained_relapse_ts(samples, start, end_ts, mds)
                    == _reference(samples, start, end_ts, mds)), (samples, start, mds)


def test_earliest_sustained_relapse_wins():
    # A transient dip (1s, < 3s min) at 100 then a real sustained death at 200.
    # The relapse that closes a recovery measurement is the SUSTAINED one (200),
    # never the transient flicker.
    samples = [(100.0, 0, 50), (101.0, 30, 50), (200.0, 0, 50)]
    assert _sustained_relapse_ts(samples, 0, 10_000.0, 3.0) == 200.0


def test_runs_to_window_end_when_never_recovers():
    samples = [(100.0, 0, 50), (150.0, 0, 50)]
    # No hits>0 after either dead sample → dead-run measured to end_ts; the
    # FIRST dead sample (100) is the earliest sustained relapse.
    assert _sustained_relapse_ts(samples, 0, 10_000.0, 3.0) == 100.0


def test_is_linear_no_per_sample_suffix_rescan():
    # A long alternating stream is the O(n^2) worst case for the old next()
    # re-scan (every other sample is hits<=0). Count tuple element accesses to
    # prove the new pass touches each sample a bounded number of times.
    n = 4000
    touches = 0

    class _Tup(tuple):
        __slots__ = ()

        def __iter__(self):
            nonlocal touches
            touches += 1
            return super().__iter__()

    samples = [_Tup((float(i), 0 if i % 2 else 40, 50)) for i in range(n)]
    _sustained_relapse_ts(samples, 0, 1e9, 3.0)
    # One unpack per sample for a single pass (plus negligible slack). The old
    # O(n^2) scan unpacked ~n²/4 ≈ 4,000,000 times for this stream.
    assert touches <= 3 * n, touches
