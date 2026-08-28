"""Lyrics acquisition: cache-first LRC fetch with pluggable providers.
Preserves the original Caelestia-mirror behavior so `mysong-fix`/
`mysong-retry` and the Caelestia lyric widget keep working unchanged."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .cache import TextCache
from .detect import Song

log = logging.getLogger(__name__)

try:
    import syncedlyrics
    HAVE_SYNCEDLYRICS = True
except ImportError:
    HAVE_SYNCEDLYRICS = False


@dataclass(frozen=True)
class LyricLine:
    timestamp: float
    text: str


def parse_lrc(text: str) -> List[LyricLine]:
    lines: List[LyricLine] = []
    for raw in text.splitlines():
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)", raw)
        if not m:
            continue
        minutes, seconds, content = m.groups()
        try:
            ts = int(minutes) * 60 + float(seconds)
        except ValueError:
            continue
        lines.append(LyricLine(timestamp=ts, text=content.strip()))
    return sorted(lines, key=lambda l: l.timestamp)


class LyricsService:
    def __init__(self, cache: TextCache, providers: Sequence[str],
                 caelestia_dir: Optional[Path] = None):
        self.cache = cache
        self.providers = list(providers)
        self.caelestia_dir = caelestia_dir

    def fetch(self, song: Song, force_retry: bool = False) -> List[LyricLine]:
        key = song.key
        if force_retry:
            self.cache.evict(key)

        text = None if force_retry else self.cache.read(key)

        if text is None and HAVE_SYNCEDLYRICS:
            text = self._scrape(song)
            if text:
                self.cache.write(key, text)

        if not text:
            return []

        self._sync_caelestia(song, text)
        return parse_lrc(text)

    def _scrape(self, song: Song) -> str:
        # A low-confidence artist (guessed from an ambiguous browser-tab
        # title, possibly the uploader/channel name) does more harm than
        # good in a fuzzy-match query — drop it and search by title alone.
        if song.artist_confidence == "low":
            query = song.title.strip()
        else:
            query = f"{song.title} {song.artist}".strip()
        try:
            return syncedlyrics.search(query, providers=self.providers) or ""
        except Exception as e:  # provider errors are numerous and non-fatal
            log.debug("lyric scrape failed for %r: %s", query, e)
            return ""

    def _sync_caelestia(self, song: Song, lrc_text: str) -> None:
        if not self.caelestia_dir or not self.caelestia_dir.exists():
            return
        try:
            filename = f"{song.artist.lower().strip()} - {song.title.lower().strip()}.lrc"
            (self.caelestia_dir / filename).write_text(lrc_text, encoding="utf-8")
        except OSError as e:
            log.debug("caelestia sync failed: %s", e)


def current_and_next(
    lines: List[LyricLine], position: float, max_hold_seconds: Optional[float] = None
) -> Tuple[str, str]:
    """Returns (current_line_text, next_line_text) for the given playback
    position. `max_hold_seconds`, if given, clears `current` back to "" once
    playback has moved more than that far past the current line's own
    timestamp with no next line yet reached — otherwise a line stays
    displayed forever through any instrumental break or outro after it,
    including the one at the very end of the song, since LRC timestamps
    only mark when a line *starts*, never when it ends."""
    current, nxt = "", ""
    current_ts: Optional[float] = None
    for i, line in enumerate(lines):
        if position >= line.timestamp:
            current = line.text
            current_ts = line.timestamp
            nxt = lines[i + 1].text if i + 1 < len(lines) else ""
        else:
            break

    if max_hold_seconds is not None and current_ts is not None:
        if position - current_ts > max_hold_seconds:
            current = ""

    return current, nxt
