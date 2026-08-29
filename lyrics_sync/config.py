"""Configuration for lyrics-sync. All tunables live here as frozen dataclasses
so the rest of the app never reaches for a global constant."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple


@dataclass(frozen=True)
class SyncConfig:
    resync_drift_seconds: float = 0.35   # drift beyond this triggers smoothing
    poll_interval_seconds: float = 0.5   # how often we ask playerctl for ground truth
    seek_jump_threshold: float = 1.0     # drift beyond this is treated as a real seek
    clock_tick_seconds: float = 0.05     # render-loop tick (local extrapolated clock)


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
    outline: bool = False
    shadow: bool = False
    fade_frames: int = 4
    # Mode 2 (`lyrics 2`): reveal each line character-by-character instead
    # of swapping the whole line in at once. See main.py's render_loop.
    typing_effect: bool = False
    typing_chars_per_second: float = 20.0

    # Layout: one of renderer.py's DISPLAY_* constants. Purely a layout/
    # framing choice — independent of `theme` below, so e.g. hacker-matrix
    # framing with a different color theme is a valid combination.
    display_mode: str = "minimalist"

    # Color: one of the keys in renderer.THEMES. Only takes effect when
    # gradient=True and rainbow=False — rainbow still wins if both are
    # on, same precedence as before this was added.
    theme: str = "classic_mono"

    # Opt-in legibility feature, NOT a rendering necessity — fonts.py's
    # RasterUnicodeFont already renders any script correctly via Pillow.
    # This is for people who'd rather see "Konnichiwa" than kanji they
    # can't read. See languages.py.
    romanize: bool = False


@dataclass(frozen=True)
class Paths:
    home: Path = field(default_factory=lambda: Path(os.path.expanduser("~/Lyrics-Sync")))
    lyrics_cache: Path = field(init=False)
    transcript_cache: Path = field(init=False)
    glyph_cache: Path = field(init=False)
    fonts_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "lyrics_cache", self.home / "lyrics")
        object.__setattr__(self, "transcript_cache", self.home / "transcripts")
        object.__setattr__(self, "glyph_cache", self.home / "glyph_cache")
        object.__setattr__(self, "fonts_dir", self.home / "fonts")

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
    fetch_timeout_seconds: float = 20.0     # bound on lyric-scrape time; a slow/hung
                                             # provider can never freeze the app past this
    ai_enabled: bool = True                 # auto-disables if deps missing; local-only, see ai.py
    ollama_model: str = "llama3.2:1b"       # must already be pulled: `ollama pull llama3.2:1b`
    ollama_host: str = "http://localhost:11434"
    song_id_timeout_seconds: float = 4.0    # bound on the AI disambiguation call

    # Last-resort audio-fingerprint identification (see fingerprint.py) for
    # content with no usable text clue at all. OFF by default — leave the
    # key empty to disable. Get a free key at https://acoustid.org/api-key.
    # Unlike the Ollama assist above, this sends a compact audio fingerprint
    # (not raw audio) to a third-party lookup service (AcoustID) — a real
    # network dependency, not local-only.
    acoustid_api_key: str = ""
    acoustid_sample_seconds: float = 8.0
    acoustid_timeout_seconds: float = 6.0
    providers: Tuple[str, ...] = (
        "LrcLib", "NetEase", "Megalobiz", "Musixmatch", "Lyricsify", "Genius",
    )
