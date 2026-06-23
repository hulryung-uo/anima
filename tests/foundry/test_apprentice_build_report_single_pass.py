"""build_report must run the death-interval analysis ONCE.

``_alive_fraction`` and ``_longest_alive_stretch`` derive from the SAME true
death intervals ``analyze_deaths`` returns. Before this fix ``build_report``
called ``analyze_deaths`` directly AND each helper re-ran it internally — three
full O(n) scans of the vitals stream per report (the redundancy
``_sustained_relapse_ts``'s own comment flags as the cost that compounds on a
long contested soak). The deaths list is now computed once and threaded into the
helpers; the reported metrics must be byte-for-byte unchanged.
"""
import foundry.apprentice as apprentice
from foundry.apprentice import (
    _alive_fraction,
    _longest_alive_stretch,
    analyze_deaths,
    build_report,
)
from foundry.kernel.trajectory import TrajectorySummary


def _summary(hp_samples, start=1000.0, end=2800.0):  # 1800s = 0.5h window
    return TrajectorySummary(start_ts=start, end_ts=end, hp_samples=hp_samples)


def test_build_report_calls_analyze_deaths_exactly_once(monkeypatch):
    # Two real deaths (one rescued, one intervention) so every helper has work.
    s = _summary([(1000, 50, 50), (1100, 0, 50), (1150, 40, 50),
                  (2000, 0, 50), (2400, 30, 50)])
    real = apprentice.analyze_deaths
    calls = {"n": 0}

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(apprentice, "analyze_deaths", _counting)
    build_report(s, "adventurer", "warrior", grace_s=180)
    assert calls["n"] == 1, (
        f"build_report ran analyze_deaths {calls['n']}x; the shared deaths list "
        "must be computed once and threaded into the helpers"
    )


def test_threaded_deaths_match_independent_computation():
    # The helpers must produce the SAME number whether they compute the deaths
    # themselves or are handed the pre-computed list.
    s = _summary([(1000, 50, 50), (1100, 0, 50), (1150, 40, 50),
                  (2000, 0, 50), (2400, 30, 50)])
    deaths = analyze_deaths(s, grace_s=180)

    assert _alive_fraction(s, grace_s=180) == _alive_fraction(
        s, grace_s=180, deaths=deaths)
    assert _longest_alive_stretch(s, grace_s=180) == _longest_alive_stretch(
        s, grace_s=180, deaths=deaths)


def test_metrics_unchanged_by_single_pass():
    # Regression guard on the actual figures (same shape as the existing
    # test_autonomy_metrics case): the optimisation must not move any number.
    s = _summary([(1000, 50, 50), (1100, 0, 50), (1150, 40, 50),
                  (2000, 0, 50), (2400, 30, 50)])
    r = build_report(s, "adventurer", "warrior", grace_s=180)
    assert r.deaths == 2
    assert r.self_rescued == 1
    assert r.shadow_interventions == 1
    assert r.self_rescue_rate == 0.5
    assert r.interventions_per_hour == 2.0
    assert r.alive_fraction == round(1.0 - (50 + 400) / 1800.0, 3)
    # alive stretches: [1000,1100]=100, [1150,2000]=850, [2400,2800]=400 → 850
    assert r.longest_alive_stretch_s == 850.0


def test_single_pass_holds_with_no_deaths():
    # A clean run still touches analyze_deaths only once.
    s = _summary([(1000, 50, 50), (2800, 50, 50)])
    real = apprentice.analyze_deaths
    calls = {"n": 0}

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch_target = apprentice
    orig = monkeypatch_target.analyze_deaths
    monkeypatch_target.analyze_deaths = _counting
    try:
        r = build_report(s, "mage", "mage")
    finally:
        monkeypatch_target.analyze_deaths = orig
    assert calls["n"] == 1
    assert r.deaths == 0 and r.alive_fraction == 1.0
