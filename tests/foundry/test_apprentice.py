"""Apprentice soak analyzer (A1) — pure-function tests over synthetic
hp_samples timelines (no shard). Validates death/recovery reconstruction and
the autonomy metrics that measure Rank 6 (flee/recover) over a long horizon.
"""
from foundry.apprentice import analyze_deaths, build_report, _longest_alive_stretch
from foundry.kernel.trajectory import TrajectorySummary


def _summary(hp_samples, start=1000.0, end=2800.0):  # 1800s = 0.5h window
    return TrajectorySummary(start_ts=start, end_ts=end, hp_samples=hp_samples)


class TestAnalyzeDeaths:
    def test_no_death(self):
        s = _summary([(1000, 50, 50), (1500, 30, 50), (2800, 45, 50)])
        assert analyze_deaths(s) == []

    def test_transient_hp0_blip_not_counted(self):
        # hits 0 then 1 one second later → transient reading, not a ghost death
        # (a real death stays a ghost until a healer resurrection, many seconds).
        s = _summary([(1000, 50, 50), (1542, 0, 71), (1543, 1, 71)])
        assert analyze_deaths(s) == []

    def test_self_rescued_within_grace(self):
        s = _summary([(1000, 50, 50), (1100, 0, 50), (1200, 40, 50)])  # dead 100s
        ev = analyze_deaths(s, grace_s=180)
        assert len(ev) == 1
        assert ev[0].self_rescued is True
        assert ev[0].needed_intervention is False
        assert ev[0].dead_s == 100

    def test_needs_intervention_when_dead_past_grace(self):
        s = _summary([(1000, 50, 50), (1100, 0, 50), (1400, 40, 50)])  # dead 300s
        ev = analyze_deaths(s, grace_s=180)
        assert ev[0].self_rescued is False
        assert ev[0].needed_intervention is True

    def test_never_revived_runs_to_window_end(self):
        s = _summary([(1000, 50, 50), (1100, 0, 50)])  # dead until end (1700s)
        ev = analyze_deaths(s, grace_s=180)
        assert len(ev) == 1
        assert ev[0].revived_ts is None
        assert ev[0].needed_intervention is True
        assert ev[0].dead_s == 1700

    def test_multiple_deaths(self):
        s = _summary([(1000, 50, 50), (1100, 0, 50), (1150, 40, 50),   # rescued (50s)
                      (2000, 0, 50), (2400, 30, 50)])                   # intervention (400s)
        ev = analyze_deaths(s, grace_s=180)
        assert len(ev) == 2
        assert ev[0].self_rescued and not ev[1].self_rescued


class TestLongestAliveStretch:
    def test_picks_longer_segment_around_death(self):
        # alive 1000-1100 (100s), dead, alive 1200-2800 (1600s) → 1600
        s = _summary([(1000, 50, 50), (1100, 0, 50), (1200, 40, 50)])
        assert _longest_alive_stretch(s) == 1600

    def test_no_samples_assumes_full_duration(self):
        assert _longest_alive_stretch(_summary([])) == 1800


class TestBuildReport:
    def test_autonomy_metrics(self):
        s = _summary([(1000, 50, 50), (1100, 0, 50), (1150, 40, 50),
                      (2000, 0, 50), (2400, 30, 50)])
        r = build_report(s, "adventurer", "warrior", grace_s=180)
        assert r.deaths == 2
        assert r.self_rescued == 1
        assert r.shadow_interventions == 1
        assert r.self_rescue_rate == 0.5
        assert r.interventions_per_hour == 2.0   # 1 intervention / 0.5h
        assert r.duration_h == 0.5

    def test_clean_run_reports_full_autonomy(self):
        s = _summary([(1000, 50, 50), (2800, 50, 50)])
        r = build_report(s, "mage", "mage")
        assert r.deaths == 0
        assert r.shadow_interventions == 0
        assert r.self_rescue_rate == 1.0
        assert r.interventions_per_hour == 0.0
