"""PingTracker must difference each pong against ITS OWN ping's send instant.

ping_loop and recv_loop interleave, so more than one ping can be in flight. A
single shared _send_time let a later on_send clobber the value an in-flight pong
needed, yielding a wrong/negative latency. The tracker keys the send instant by
seq slot; these tests pin that correlation with a controllable fake clock.
"""
import pytest

import anima.main as main_mod
from anima.main import PingTracker


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t


def _patch_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(main_mod.asyncio, "get_event_loop", lambda: clock)
    return clock


def test_overlapping_pings_measure_against_own_send(monkeypatch):
    clock = _patch_clock(monkeypatch)
    t = PingTracker()

    clock.t = 1.0
    seq_a = t.on_send()          # ping A sent at t=1.0
    clock.t = 2.0
    seq_b = t.on_send()          # ping B sent at t=2.0 (A still in flight)
    assert seq_a != seq_b

    # Pong A arrives at t=6.2 → 5.2s = 5200ms, measured against A's OWN send.
    clock.t = 6.2
    t.on_recv(seq_a)
    assert t._pings[seq_a] == 5200.0

    # Pong B arrives at t=2.5 → 0.5s = 500ms against B's send (not A's).
    clock.t = 2.5
    t.on_recv(seq_b)
    assert t._pings[seq_b] == 500.0


def test_duplicate_echo_does_not_recredit(monkeypatch):
    clock = _patch_clock(monkeypatch)
    t = PingTracker()
    clock.t = 1.0
    seq = t.on_send()
    clock.t = 1.1
    t.on_recv(seq)               # 100ms
    assert t._pings[seq] == pytest.approx(100.0)
    # A duplicate echo for the same seq (no outstanding ping) is dropped, not
    # re-differenced against a stale instant.
    clock.t = 9.0
    t.on_recv(seq)
    assert t._pings[seq] == pytest.approx(100.0)


def test_latency_ms_averages_only_recorded_slots(monkeypatch):
    clock = _patch_clock(monkeypatch)
    t = PingTracker()
    clock.t = 1.0
    s1 = t.on_send()
    clock.t = 1.1
    t.on_recv(s1)                # 100ms
    clock.t = 2.0
    s2 = t.on_send()
    clock.t = 2.3
    t.on_recv(s2)                # 300ms
    assert t.latency_ms == pytest.approx(200.0)  # (100 + 300) / 2
