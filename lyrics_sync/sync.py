"""SyncEngine: authoritative local playback clock reconciled against playerctl.

Continuous timing never comes straight from playerctl — polling it every
render tick is both slow (D-Bus round trip) and jittery (player scheduling,
browser throttling). Instead we run our own monotonic clock, extrapolate
from the last checkpoint, and only consult playerctl periodically (or on a
play/pause edge) to correct drift.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from .config import SyncConfig
from .player import PlayerState


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


class SyncEngine:
    def __init__(self, config: SyncConfig):
        self.cfg = config
        self._pos_ref: float = 0.0
        self._wall_ref: float = time.monotonic()
        self._playing: bool = False
        self._song_key: Optional[str] = None
        self._last_poll: float = 0.0

        # Effective thresholds actually used by tick() — start at the
        # configured defaults, but adapt_to_lyrics() can tighten them once
        # we know how closely-spaced the current song's lyric lines are.
        # Kept separate from self.cfg so the original config is never
        # mutated and always available as the "loosest" fallback.
        self._resync_drift = config.resync_drift_seconds
        self._poll_interval = config.poll_interval_seconds

    def adapt_to_lyrics(self, min_line_gap: Optional[float]) -> None:
        """Tighten (or reset) drift-correction precision based on how fast
        the current song's lyrics move.

        For a typical song, the configured defaults are already more than
        precise enough — but a fast verse with lines 0.3–0.5s apart can have
        the *default* resync_drift_seconds (0.35s) smooth right past a line
        boundary, showing the wrong line for a moment. Capping the
        effective drift threshold (and poll interval, so corrections happen
        often enough to matter) to a fraction of the smallest real gap
        between lines fixes that — while never being *looser* than the
        configured defaults, so slow songs are unaffected.

        Pass None to reset to the configured defaults (e.g. when a new song
        starts and we don't know its line spacing yet).
        """
        if min_line_gap is None or min_line_gap <= 0:
            self._resync_drift = self.cfg.resync_drift_seconds
            self._poll_interval = self.cfg.poll_interval_seconds
            return
        # Floor of 0.05s keeps this sane for pathological/malformed LRC data
        # with near-duplicate timestamps; cap at the configured default so
        # this only ever tightens precision, never loosens it.
        self._resync_drift = max(0.05, min(self.cfg.resync_drift_seconds, min_line_gap * 0.3))
        self._poll_interval = max(0.15, min(self.cfg.poll_interval_seconds, min_line_gap * 0.5))

    def _estimate(self) -> float:
        if not self._playing:
            return self._pos_ref
        return self._pos_ref + (time.monotonic() - self._wall_ref)

    def reset(self, song_key: str, state: Optional[PlayerState]) -> SyncSnapshot:
        self._song_key = song_key
        self._pos_ref = state.position if state else 0.0
        self._wall_ref = time.monotonic()
        self._playing = state.playing if state else False
        self._last_poll = time.monotonic()
        self.adapt_to_lyrics(None)  # unknown line spacing until lyrics load
        return SyncSnapshot(self._pos_ref, self._playing, TransportEvent.SONG_CHANGE)

    def tick(self, state: Optional[PlayerState], song_key: Optional[str]) -> SyncSnapshot:
        now = time.monotonic()

        if state is None:
            self._playing = False
            return SyncSnapshot(self._estimate(), False, TransportEvent.STOPPED)

        if song_key is not None and song_key != self._song_key:
            return self.reset(song_key, state)

        if state.playing != self._playing:
            # Instant pause/play — trusted immediately, not gated by poll cadence,
            # so pausing/resuming never feels laggy.
            self._pos_ref = state.position
            self._wall_ref = now
            self._playing = state.playing
            self._last_poll = now
            event = TransportEvent.PLAY if state.playing else TransportEvent.PAUSE
            return SyncSnapshot(self._pos_ref, self._playing, event)

        event = TransportEvent.NONE
        if (now - self._last_poll) >= self._poll_interval:
            estimate = self._estimate()
            drift = state.position - estimate
            self._last_poll = now

            if abs(drift) > self.cfg.seek_jump_threshold:
                # Big jump: a real seek/rewind/skip — snap instantly. Always
                # checked against the *configured* threshold, not the
                # adaptive one, so a deliberate skip is never mistaken for
                # drift regardless of how tightly tuned the song is.
                self._pos_ref = state.position
                self._wall_ref = now
                event = TransportEvent.SEEK
            elif abs(drift) > self._resync_drift:
                # Small accumulated drift (buffering, clock skew): half-step
                # correction so the displayed lyric never visibly jumps.
                self._pos_ref = estimate + drift * 0.5
                self._wall_ref = now

        return SyncSnapshot(self._estimate(), self._playing, event)
