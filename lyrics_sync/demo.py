"""Demo mode: try every display mode and animation without a music player.

`termilyrics --demo` plays a built-in, word-timed rendition of "Twinkle,
Twinkle, Little Star" (Jane Taylor, 1806 — public domain) against a local
clock. `--demo song.lrc` plays your own LRC file the same way (handy for
checking an LRC's timing), and `--speed 0.8` pretends the track is a 0.8x
"slowed + reverb" upload, to see the tempo re-timing in action.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List, Optional

from .detect import Song
from .lyrics import LrcDocument, LyricsResult, parse_lrc_document
from .player import PlayerState

_BEAT = 0.6  # 100 BPM


def _line(start_beat: float, words: List[tuple]) -> str:
    """words: (text, beat offset within the line). Returns an enhanced-LRC line."""
    def ts(beats: float) -> str:
        sec = beats * _BEAT
        return f"{int(sec // 60):02d}:{sec % 60:05.2f}"
    body = " ".join(f"<{ts(start_beat + off)}>{text}" for text, off in words)
    end = ts(start_beat + 8)
    return f"[{ts(start_beat)}]{body} <{end}>"


def _build_demo_lrc() -> str:
    verse = [
        [("Twinkle,", 0), ("twinkle,", 2), ("little", 4), ("star,", 6)],
        [("How", 0), ("I", 1), ("wonder", 2), ("what", 4), ("you", 5), ("are!", 6)],
        [("Up", 0), ("above", 1), ("the", 3), ("world", 4), ("so", 5), ("high,", 6)],
        [("Like", 0), ("a", 1), ("diamond", 2), ("in", 4), ("the", 5), ("sky.", 6)],
        [("Twinkle,", 0), ("twinkle,", 2), ("little", 4), ("star,", 6)],
        [("How", 0), ("I", 1), ("wonder", 2), ("what", 4), ("you", 5), ("are!", 6)],
    ]
    verse2 = [
        [("When", 0), ("the", 1), ("blazing", 2), ("sun", 4), ("is", 5), ("gone,", 6)],
        [("When", 0), ("he", 1), ("nothing", 2), ("shines", 4), ("upon,", 5)],
        [("Then", 0), ("you", 1), ("show", 2), ("your", 3), ("little", 4), ("light,", 6)],
        [("Twinkle,", 0), ("twinkle,", 2), ("all", 4), ("the", 5), ("night.", 6)],
    ]
    out = ["[ti:Twinkle, Twinkle, Little Star]", "[ar:Jane Taylor (demo)]"]
    beat = 8.0  # one line's worth of intro
    for words in verse:
        out.append(_line(beat, words))
        beat += 8
    out.append(f"[{int(beat * _BEAT // 60):02d}:{beat * _BEAT % 60:05.2f}]")  # instrumental break
    beat += 16
    for words in verse2:
        out.append(_line(beat, words))
        beat += 8
    out.append(f"[{int(beat * _BEAT // 60):02d}:{beat * _BEAT % 60:05.2f}]")
    return "\n".join(out)


DEMO_LRC = _build_demo_lrc()


class DemoPlayer:
    """Stands in for PlayerSource: a looping local clock."""

    def __init__(self, doc: LrcDocument, title: str, artist: str, speed: float = 1.0):
        self.speed = speed if speed > 0 else 1.0
        last = max((l.timestamp for l in doc.lines), default=0.0)
        self.original_length = doc.length or (last + 4.0)
        self.length = self.original_length / self.speed
        tag = ""
        if self.speed < 0.99:
            tag = " (slowed + reverb)"
        elif self.speed > 1.01:
            tag = " (sped up)"
        self.title = title + tag
        self.artist = artist
        self._start = time.monotonic()
        self._paused_at: Optional[float] = None

    def _position(self, now: float) -> float:
        ref = self._paused_at if self._paused_at is not None else now
        return (ref - self._start) % self.length

    def read(self) -> Optional[PlayerState]:
        now = time.monotonic()
        return PlayerState(player="demo", title=self.title, artist=self.artist,
                           position=self._position(now),
                           status="Paused" if self._paused_at is not None else "Playing",
                           length=self.length, sampled_at=now)

    def command(self, player: Optional[str], action: str) -> bool:
        now = time.monotonic()
        if action in ("play-pause", "pause", "play"):
            if self._paused_at is None and action != "play":
                self._paused_at = now
            elif self._paused_at is not None and action != "pause":
                self._start += now - self._paused_at
                self._paused_at = None
            return True
        if action in ("next", "previous"):
            self._start = now if self._paused_at is None else self._paused_at
            return True
        return False


class DemoLyricsSource:
    """Stands in for LyricsService: always returns the demo document."""

    def __init__(self, doc: LrcDocument, original_length: float):
        self.doc = doc
        self.original_length = original_length

    def fetch(self, song: Song, force_retry: bool = False, player_length: Optional[float] = None) -> LyricsResult:
        return LyricsResult(lines=list(self.doc.lines), source="demo", duration=self.original_length,
                            matched_title=song.title, matched_artist=song.artist)

    def fetch_word_timing(self, song: Song, base) -> None:
        return None

    def lookup_duration(self, song: Song) -> Optional[float]:
        return self.original_length


def load_demo(path: Optional[str], speed: float = 1.0):
    """(DemoPlayer, DemoLyricsSource) for the built-in song or an LRC file."""
    if path:
        text = Path(os.path.expanduser(path)).read_text(encoding="utf-8")
        doc = parse_lrc_document(text)
        title = doc.title or Path(path).stem
        artist = doc.artist or "LRC file"
    else:
        doc = parse_lrc_document(DEMO_LRC)
        title, artist = doc.title, doc.artist
    player = DemoPlayer(doc, title, artist, speed)
    return player, DemoLyricsSource(doc, player.original_length)
