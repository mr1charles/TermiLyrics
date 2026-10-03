"""Configuration for lyrics-sync. All tunables live here as frozen dataclasses
so the rest of the app never reaches for a global constant."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple


@dataclass(frozen=True)
class SyncConfig:
    # Upper bound on the dead-band below which a playerctl reading is
    # treated as IPC noise rather than real drift. The engine tightens this
    # automatically for clean players and fast songs (see sync.py).
    resync_drift_seconds: float = 0.35
    poll_interval_seconds: float = 0.4   # how often we ask playerctl for ground truth
    seek_jump_threshold: float = 1.0     # drift beyond this is treated as a real seek
    clock_tick_seconds: float = 0.05     # idle render-loop tick
    correction_gain: float = 0.6         # fraction of real drift corrected per poll
    # Learn the player's actual playback speed (e.g. YouTube set to 0.75x)
    # from successive position readings instead of assuming 1.0x.
    estimate_rate: bool = True
    # Applied to every song, on top of per-song adjustments. Positive values
    # show lyrics earlier — useful to compensate for a slow audio sink
    # (Bluetooth headphones are typically 0.15–0.3s behind).
    global_offset_seconds: float = 0.0


@dataclass(frozen=True)
class AlignmentConfig:
    min_token_overlap: float = 0.35
    min_ratio: float = 0.55
    window_seconds: float = 12.0   # forward search window per lyric line
    duplicate_guard_seconds: float = 2.0


@dataclass(frozen=True)
class RenderConfig:
    min_block_cols: int = 12
    font_height: int = 5
    glyph_gap: int = 2
    rainbow: bool = False
    gradient: bool = True
    # outline/shadow/fade_frames: accepted for compatibility with older
    # configs; the canvas renderer's themes and animations replace them.
    outline: bool = False
    shadow: bool = False
    fade_frames: int = 4
    # Legacy switch for mode 2 (`termilyrics 2`) — now equivalent to
    # animation="typewriter". Kept so old configs/aliases keep working.
    typing_effect: bool = False
    typing_chars_per_second: float = 20.0

    # Layout: one of renderer.DISPLAY_MODES. Purely a layout/framing
    # choice — independent of `theme` below.
    display_mode: str = "minimalist"

    # Color: one of the keys in themes.THEMES.
    theme: str = "classic_mono"

    # Line-change transition: one of renderer.ANIMATIONS.
    animation: str = "fade"

    # auto / block / medium / plain — see renderer.TEXT_STYLES.
    text_style: str = "auto"

    # Target frame rate while something is actually moving (bouncing ball,
    # word sweep, transitions). Static frames are never redrawn.
    fps: int = 30

    # One-row footer with song, sync adjustments and a progress bar.
    status_bar: bool = True

    # Opt-in legibility feature for non-Latin scripts — see languages.py and
    # Renderer._prepare_text for the fallback chain. Default ON because
    # non-Latin scripts render unreliably through the raster font fallback
    # unless the system happens to have a suitable font installed.
    romanize: bool = True


@dataclass(frozen=True)
class Paths:
    home: Path = field(default_factory=lambda: Path(os.path.expanduser("~/Lyrics-Sync")))
    lyrics_cache: Path = field(init=False)
    transcript_cache: Path = field(init=False)
    glyph_cache: Path = field(init=False)
    fonts_dir: Path = field(init=False)
    settings_file: Path = field(init=False)
    adjustments_file: Path = field(init=False)
    log_file: Path = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "lyrics_cache", self.home / "lyrics")
        object.__setattr__(self, "transcript_cache", self.home / "transcripts")
        object.__setattr__(self, "glyph_cache", self.home / "glyph_cache")
        object.__setattr__(self, "fonts_dir", self.home / "fonts")
        object.__setattr__(self, "settings_file", self.home / "settings.json")
        object.__setattr__(self, "adjustments_file", self.home / "adjustments.json")
        object.__setattr__(self, "log_file", self.home / "termilyrics.log")

    def ensure(self) -> None:
        for p in (self.home, self.lyrics_cache, self.transcript_cache, self.glyph_cache, self.fonts_dir):
            p.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class AppConfig:
    sync: SyncConfig = field(default_factory=SyncConfig)
    alignment: AlignmentConfig = field(default_factory=AlignmentConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    paths: Paths = field(default_factory=Paths)
    preload_before_playback: bool = False   # Problem 5's "optional configurable mode"
    fetch_timeout_seconds: float = 25.0     # bound on lyric-scrape time; a slow/hung
                                             # provider can never freeze the app past this
    preferred_players: Tuple[str, ...] = ("spotify",)

    # Lyric sources. LrcLib is queried directly first (it returns the
    # recording's duration, which is how we pick the right version and how
    # slowed/sped-up tracks get re-timed); syncedlyrics covers the rest.
    lrclib_enabled: bool = True
    musicbrainz_enabled: bool = True        # duration lookup only, for re-timing variants
    word_sync_enabled: bool = True          # background upgrade to word-by-word timing
    http_timeout_seconds: float = 8.0
    user_agent: str = "TermiLyrics/0.1.2 (https://github.com/mr1charles/TermiLyrics)"

    # Slowed / sped-up / nightcore uploads: stretch the original lyric
    # timestamps to the track's real speed. See timing.variant_scale().
    auto_tempo_for_variants: bool = True

    ai_enabled: bool = True                 # auto-disables if deps missing; local-only, see ai.py
    ollama_model: str = "llama3.2:1b"       # must already be pulled: `ollama pull llama3.2:1b`
    ollama_host: str = "http://localhost:11434"
    song_id_timeout_seconds: float = 4.0    # bound on the AI disambiguation call

    # Last-resort audio-fingerprint identification (see fingerprint.py) for
    # content with no usable text clue at all. OFF by default — leave the
    # key empty to disable, or set TERMILYRICS_ACOUSTID_KEY. Get a free key
    # at https://acoustid.org/api-key. Unlike the Ollama assist above, this
    # sends a compact audio fingerprint (not raw audio) to a third-party
    # lookup service (AcoustID) — a real network dependency.
    acoustid_api_key: str = field(default_factory=lambda: os.environ.get("TERMILYRICS_ACOUSTID_KEY", ""))
    acoustid_sample_seconds: float = 15.0
    acoustid_timeout_seconds: float = 6.0
    # Also try the fingerprint at these speed-correction factors, so a
    # slowed (x0.8) or sped-up (x1.25) upload still matches the original
    # recording. Needs ffmpeg. Empty tuple disables it.
    acoustid_tempo_factors: Tuple[float, ...] = (1.25, 1.18, 1.33, 1.11, 0.8, 0.87, 0.75)

    providers: Tuple[str, ...] = (
        "LrcLib", "NetEase", "Megalobiz", "Musixmatch", "Lyricsify", "Genius",
    )
