"""SyncEngine: authoritative local playback clock reconciled against playerctl.

Continuous timing never comes straight from playerctl — polling it every
render tick is both slow (a process spawn plus a D-Bus round trip) and
jittery. Instead we run our own monotonic clock, extrapolate from the last
checkpoint, and only use playerctl readings to correct it:

  * Every reading carries the monotonic time its position was *sampled*
    (player.PlayerState.sampled_at), so the time it took to spawn
    playerctl, identify the song, etc. is never mistaken for playback.
  * Playback rate is learned, not assumed. A least-squares fit over the
    last few seconds of readings gives the player's real speed — so a
    YouTube video at 0.75x (or a player that runs slightly fast) is
    extrapolated correctly between polls instead of drifting and being
    yanked back every half second.
  * Small drift is corrected proportionally and only outside a dead-band
    sized from the player's own measured noise; large jumps are seeks and
    snap immediately.
  * position() never visibly runs backwards for a small correction — it
    holds still for a moment instead, which matters once individual words
    are being highlighted.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import Callable, Deque, Optional, Tuple

from .config import SyncConfig
from .player import PlayerState

_COMMON_RATES = (0.25, 0.5, 0.75, 0.8, 0.85, 0.9, 1.1, 1.15, 1.2, 1.25, 1.5, 1.75, 2.0)


class TransportEvent(Enum):
    NONE = auto()
    PLAY = auto()
    PAUSE = auto()
    SEEK = auto()
    SONG_CHANGE = auto()
    STOPPED = auto()


@dataclass(frozen=True)
class SyncSnapshot:
    position: float
    playing: bool
    event: TransportEvent
    rate: float = 1.0


class SyncEngine:
    def __init__(self, config: SyncConfig, clock: Callable[[], float] = time.monotonic):
        self.cfg = config
        self._clock = clock
        self._pos_ref: float = 0.0
        self._wall_ref: float = clock()
        self._playing: bool = False
        self._song_key: Optional[str] = None
        self._rate: float = 1.0
        self._history: Deque[Tuple[float, float]] = deque(maxlen=48)
        self._noise: Deque[float] = deque(maxlen=12)
        self._last_sample_at: float = -math.inf
        self._last_out: Optional[float] = None

        # Effective thresholds — start at the configured defaults, but
        # adapt_to_lyrics() can tighten them once we know how closely
        # spaced the current song's lines are. Kept separate from self.cfg
        # so the original config is never mutated.
        self._resync_drift = config.resync_drift_seconds
        self._poll_interval = config.poll_interval_seconds

    # ---- properties ----

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    def adapt_to_lyrics(self, min_line_gap: Optional[float]) -> None:
        """Tighten (or reset) drift-correction precision based on how fast
        the current song's lyrics move. A fast verse with lines 0.3–0.5s
        apart needs corrections well under that; slow songs are unaffected
        because this never loosens past the configured defaults. Pass None
        to reset (new song, spacing unknown)."""
        if min_line_gap is None or min_line_gap <= 0:
            self._resync_drift = self.cfg.resync_drift_seconds
            self._poll_interval = self.cfg.poll_interval_seconds
            return
        self._resync_drift = max(0.05, min(self.cfg.resync_drift_seconds, min_line_gap * 0.3))
        self._poll_interval = max(0.15, min(self.cfg.poll_interval_seconds, min_line_gap * 0.5))

    # ---- clock ----

    def _estimate(self, now: float) -> float:
        if not self._playing:
            return self._pos_ref
        return self._pos_ref + (now - self._wall_ref) * self._rate

    def _anchor(self, position: float, at: float) -> None:
        self._pos_ref = position
        self._wall_ref = at
        self._last_out = None  # a deliberate jump may go backwards

    def position(self, now: Optional[float] = None) -> float:
        est = self._estimate(self._clock() if now is None else now)
        if self._playing and self._last_out is not None and 0 < self._last_out - est < 0.3:
            return self._last_out
        self._last_out = est
        return est

    def snapshot(self, now: Optional[float] = None) -> SyncSnapshot:
        return SyncSnapshot(self.position(now), self._playing, TransportEvent.NONE, self._rate)

    # ---- ground truth ----

    def reset(self, song_key: str, state: Optional[PlayerState]) -> SyncSnapshot:
        self._song_key = song_key
        self._history.clear()
        self._noise.clear()
        now = self._clock()
        if state is not None:
            t = state.sampled_at or now
            self._anchor(state.position, t)
            self._playing = state.playing
            self._last_sample_at = t
            if state.playing:
                self._history.append((t, state.position))
        else:
            self._anchor(0.0, now)
            self._playing = False
        # The playback rate is a player setting, not a song property — a
        # YouTube speed setting carries over to the next video — so it is
        # deliberately kept across songs.
        self.adapt_to_lyrics(None)
        return SyncSnapshot(self.position(now), self._playing, TransportEvent.SONG_CHANGE, self._rate)

    def observe(self, state: PlayerState) -> TransportEvent:
        """Feed one playerctl reading. Returns what it revealed."""
        t = state.sampled_at or self._clock()
        if t <= self._last_sample_at:
            return TransportEvent.NONE  # same reading seen twice: no new information
        self._last_sample_at = t

        if state.playing != self._playing:
            # Play/pause is trusted immediately, never gated by drift logic.
            self._anchor(state.position, t)
            self._playing = state.playing
            self._history.clear()
            if state.playing:
                self._history.append((t, state.position))
            return TransportEvent.PLAY if state.playing else TransportEvent.PAUSE

        if not self._playing:
            if abs(state.position - self._pos_ref) > 0.05:
                self._anchor(state.position, t)  # scrubbing while paused
                return TransportEvent.SEEK
            return TransportEvent.NONE

        drift = state.position - self._estimate(t)
        if abs(drift) > self.cfg.seek_jump_threshold:
            # A real seek/rewind/skip — snap instantly. Checked against the
            # *configured* threshold, never the adaptive one.
            self._anchor(state.position, t)
            self._history.clear()
            self._noise.clear()
            self._history.append((t, state.position))
            return TransportEvent.SEEK

        self._history.append((t, state.position))
        self._noise.append(drift)
        self._update_rate()

        drift = state.position - self._estimate(t)
        if abs(drift) > self._deadband():
            self._pos_ref = self._estimate(t) + drift * self.cfg.correction_gain
            self._wall_ref = t
        return TransportEvent.NONE

    def tick(self, state: Optional[PlayerState], song_key: Optional[str]) -> SyncSnapshot:
        """Backwards-compatible one-call API: observe + snapshot."""
        if state is None:
            self._playing = False
            return SyncSnapshot(self.position(), False, TransportEvent.STOPPED, self._rate)
        if song_key is not None and song_key != self._song_key:
            return self.reset(song_key, state)
        event = self.observe(state)
        return replace(self.snapshot(), event=event)

    # ---- internals ----

    def _deadband(self) -> float:
        """Drift smaller than the player's own reading noise isn't drift.
        Noise is the *spread* of recent drift readings (median absolute
        deviation), not their size — a steady offset has no spread and
        must be corrected, not mistaken for jitter."""
        if len(self._noise) < 3:
            return self._resync_drift
        values = sorted(self._noise)
        med = values[len(values) // 2]
        spread = sorted(abs(v - med) for v in values)[len(values) // 2] * 1.4826
        return max(0.04, min(self._resync_drift, spread * 3.0))

    def _update_rate(self) -> None:
        if not self.cfg.estimate_rate or len(self._history) < 4:
            return
        latest = self._history[-1][0]
        pts = [(t, p) for t, p in self._history if t >= latest - 8.0]
        if len(pts) < 4 or pts[-1][0] - pts[0][0] < 2.5:
            return
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        mp = sum(p for _, p in pts) / n
        stt = sum((t - mt) ** 2 for t, _ in pts)
        if stt <= 0:
            return
        slope = sum((t - mt) * (p - mp) for t, p in pts) / stt
        resid = math.sqrt(sum((p - (mp + slope * (t - mt))) ** 2 for t, p in pts) / n)
        if resid > 0.12:
            # Either the speed changed inside the window or the player
            # reports coarse positions. Forget the older half and retry.
            for _ in range(len(self._history) // 2):
                self._history.popleft()
            return
        if not 0.25 <= slope <= 4.0:
            return
        new = 1.0 if abs(slope - 1.0) < 0.02 else slope
        for common in _COMMON_RATES:
            if abs(new - common) < 0.012:
                new = common
                break
        if abs(new - self._rate) > 0.004:
            # Re-anchor at the latest sample so the change is continuous.
            self._pos_ref = self._estimate(latest)
            self._wall_ref = latest
            self._rate = new
