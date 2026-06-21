"""Apprentice death analyzer must honour the project-wide "HP unknown" rule.

Cross-module boundary: the kernel streams ``hp_samples`` as
``(ts, hits, hits_max)``; ``anima.perception.self_state.SelfState.is_alive``
defines "alive" as ``hits > 0 or hits_max == 0`` (a 0-max sample is HP-unknown,
not dead). The apprentice autonomy analyzer used to read only ``hits`` and so
counted an unknown-HP startup run (a 0xA1 health-bar placeholder that lands
before the first full 0x11 status: hits=0, hits_max=0) as a ghost death,
fabricating deaths / interventions out of pure login latency.
"""
from foundry.apprentice import analyze_deaths, build_report, _alive_fraction
from foundry.kernel.trajectory import TrajectorySummary


def _summary(hp_samples, start=1000.0, end=2800.0):  # 1800s = 0.5h window
    return TrajectorySummary(start_ts=start, end_ts=end, hp_samples=hp_samples)


class TestHpUnknownNotADeath:
    def test_startup_unknown_hp_run_is_not_a_death(self):
        # 0xA1 placeholder (hits=0, hits_max=0) for ~500s while login/world-load
        # completes, then the first real status arrives. hits_max==0 means
        # "HP unknown" (SelfState.is_alive), NOT a ghost death — even though the
        # unknown run is far longer than MIN_DEATH_S.
        s = _summary([(1000, 0, 0), (1500, 0, 0), (1600, 50, 50), (2800, 50, 50)])
        assert analyze_deaths(s, grace_s=180) == []

    def test_unknown_hp_does_not_corrupt_autonomy_metrics(self):
        s = _summary([(1000, 0, 0), (1500, 0, 0), (1600, 50, 50), (2800, 50, 50)])
        r = build_report(s, "adventurer", "warrior", grace_s=180)
        assert r.deaths == 0
        assert r.shadow_interventions == 0
        assert r.interventions_per_hour == 0.0
        assert r.self_rescue_rate == 1.0
        # the unknown-HP startup run must not be charged as dead-time
        assert r.alive_fraction == 1.0

    def test_unknown_hp_does_not_reduce_alive_fraction(self):
        s = _summary([(1000, 0, 0), (1500, 0, 0), (1600, 50, 50), (2800, 50, 50)])
        assert _alive_fraction(s) == 1.0

    def test_real_death_with_known_max_still_counts(self):
        # Control: hits<=0 WITH a known hits_max is a genuine death and must
        # still be reported (the fix must not suppress real deaths).
        s = _summary([(1000, 50, 50), (1100, 0, 50), (1400, 40, 50)])  # dead 300s
        ev = analyze_deaths(s, grace_s=180)
        assert len(ev) == 1
        assert ev[0].needed_intervention is True
        assert ev[0].dead_s == 300

    def test_unknown_hp_relapse_does_not_reopen_a_recovered_death(self):
        # A genuine death recovers at 1200 (known max). A later unknown-HP
        # sample (hits=0, hits_max=0 — a packet that dropped its max) must NOT
        # be read as a sustained relapse that re-opens / splits the recovery.
        s = _summary([(1000, 50, 50), (1100, 0, 50), (1200, 40, 50),
                      (1700, 0, 0), (1800, 0, 0),       # unknown-HP, not a relapse
                      (2800, 50, 50)])
        ev = analyze_deaths(s, grace_s=180)
        assert len(ev) == 1
        assert ev[0].revived_ts == 1200
        assert ev[0].self_rescued is True
        assert ev[0].dead_s == 100
