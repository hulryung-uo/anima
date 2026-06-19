"""A non-default --grace must stay self-consistent across the autonomy figures.

``analyze_deaths`` drops a NEVER-REVIVED death as window-censored only when its
``dead_s <= grace_s`` (a0fa4a0). ``build_report`` already threads the caller's
``grace_s`` into ``analyze_deaths`` (the ``deaths``/``shadow_interventions``
counts) and into ``_alive_fraction``, but it used to call
``_longest_alive_stretch(summary)`` with NO grace — so that helper re-ran
``analyze_deaths`` under the DEFAULT 180s grace. With a custom ``--grace`` whose
value straddles a censored tail's ``dead_s``, the two analyzers then disagree on
whether that death exists: ``deaths`` counts it (custom grace) while
``longest_alive_stretch_s`` ignores it (default grace) and reports the FULL
window as one alive stretch — the exact contradiction every helper's docstring
swears can't happen ("from the SAME true death intervals … so the two analyzers
can't disagree"). The CLI exposes ``--grace``, so this is reachable in a soak.
"""
from foundry.apprentice import _longest_alive_stretch, build_report
from foundry.kernel.trajectory import TrajectorySummary


def _summary(hp_samples, start=1000.0, end=2800.0):  # 1800s window
    return TrajectorySummary(start_ts=start, end_ts=end, hp_samples=hp_samples)


def test_build_report_grace_threads_into_longest_alive_stretch():
    # Never-revived death opens at 2700 → dead_s = 100s. A custom grace of 90s
    # makes 100 > 90 → a CONFIRMED unrecovered death (deaths==1). The default
    # 180s grace would (wrongly, for this call) drop it as censored. The two
    # numbers must agree under the custom grace the caller chose.
    s = _summary([(1000, 50, 50), (2700, 0, 50)])
    r = build_report(s, "adventurer", "warrior", grace_s=90)

    assert r.deaths == 1
    assert r.shadow_interventions == 1
    # The death carves the timeline: the only alive stretch is 1000..2700.
    # Under the old default-grace bug this reported the full 1800s window while
    # deaths==1 — a self-contradiction.
    assert r.longest_alive_stretch_s == 1700.0
    # Consistency: a window with a confirmed death cannot have spent the WHOLE
    # window alive.
    assert r.longest_alive_stretch_s < (s.end_ts - s.start_ts)
    # alive_fraction (already grace-threaded) agrees: 100s of 1800s dead.
    assert r.alive_fraction == round(1 - 100 / 1800, 3)


def test_longest_alive_stretch_honors_passed_grace():
    # Same censored tail, exercised directly: under grace=90 the death is real
    # and chops the stretch; under grace=180 it is dropped and the whole window
    # is one alive stretch. The helper must honor the grace it is GIVEN.
    s = _summary([(1000, 50, 50), (2700, 0, 50)])
    assert _longest_alive_stretch(s, grace_s=90) == 1700.0
    assert _longest_alive_stretch(s, grace_s=180) == 1800.0


def test_default_grace_unchanged():
    # A within-default-grace censored tail (dead_s=100 <= 180) is still dropped
    # by the default call path, so the default-grace behaviour is untouched.
    s = _summary([(1000, 50, 50), (2700, 0, 50)])
    r = build_report(s, "adventurer", "warrior")  # default grace 180
    assert r.deaths == 0
    assert r.longest_alive_stretch_s == 1800.0
