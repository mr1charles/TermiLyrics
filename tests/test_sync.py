import random

from lyrics_sync.config import SyncConfig
from lyrics_sync.player import PlayerState
from lyrics_sync.sync import SyncEngine, TransportEvent


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def state(pos, t, status="Playing"):
    return PlayerState("p", "title", "artist", pos, status, sampled_at=t)


def run_player(rate, noise, polls=80, seed=3):
    clock = Clock()
    eng = SyncEngine(SyncConfig(), clock=clock)
    rng = random.Random(seed)
    eng.reset("k", state(10.0, 100.0))
    truth = lambda t: 10.0 + (t - 100.0) * rate
    errors = []
    for i in range(1, polls):
        t = 100.0 + i * 0.4
        clock.t = t + 0.01
        eng.observe(state(truth(t) + rng.gauss(0, noise), t))
        for dt in (0.1, 0.2, 0.3):
            clock.t = t + dt
            if i > 25:
                errors.append(abs(eng.position() - truth(t + dt)))
    return eng, max(errors)


def test_clean_player_is_tracked_exactly():
    eng, err = run_player(1.0, 0.0)
    assert eng.rate == 1.0 and err < 1e-6


def test_learns_non_standard_playback_rate():
    eng, err = run_player(0.75, 0.02)
    assert eng.rate == 0.75
    assert err < 0.1


def test_noisy_player_stays_within_a_tenth_of_a_second():
    _, err = run_player(1.0, 0.03)
    assert err < 0.1


def test_seek_snaps_and_pause_freezes():
    clock = Clock()
    eng = SyncEngine(SyncConfig(), clock=clock)
    eng.reset("k", state(0.0, 100.0))
    clock.t = 101.0
    assert eng.observe(state(60.0, 101.0)) == TransportEvent.SEEK
    assert abs(eng.position() - 60.0) < 1e-9
    clock.t = 102.0
    assert eng.observe(state(61.0, 102.0, "Paused")) == TransportEvent.PAUSE
    clock.t = 110.0
    assert eng.position() == 61.0


def test_sample_time_not_poll_time_is_used():
    clock = Clock(200.0)
    eng = SyncEngine(SyncConfig(), clock=clock)
    # Sampled at t=199 (position 5.0) but only handed over at t=200: 1s has passed
    eng.reset("k", state(5.0, 199.0))
    assert abs(eng.position() - 6.0) < 1e-9


def test_duplicate_reading_is_ignored():
    clock = Clock()
    eng = SyncEngine(SyncConfig(), clock=clock)
    eng.reset("k", state(0.0, 100.0))
    clock.t = 100.5
    s = state(0.5, 100.5)
    eng.observe(s)
    assert eng.observe(s) == TransportEvent.NONE


def test_small_backward_correction_holds_instead_of_rewinding():
    clock = Clock()
    eng = SyncEngine(SyncConfig(resync_drift_seconds=0.05), clock=clock)
    eng.reset("k", state(0.0, 100.0))
    for i in range(1, 8):
        clock.t = 100.0 + i * 0.4
        eng.observe(state(i * 0.4, clock.t))
    clock.t = 103.0
    before = eng.position()
    eng.observe(state(2.8 - 0.2, 103.0))  # player says we're 0.2s behind
    assert eng.position() >= before


def test_tick_compat_api():
    clock = Clock()
    eng = SyncEngine(SyncConfig(), clock=clock)
    snap = eng.tick(state(3.0, 100.0), "song")
    assert snap.event == TransportEvent.SONG_CHANGE
    assert eng.tick(None, "song").event == TransportEvent.STOPPED
