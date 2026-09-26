"""Lyric timeline: where in the lyrics a given moment is, down to the word.

Three pieces:

  * Word timing — real per-word timestamps when the provider had them
    (enhanced LRC), otherwise an estimate: each line's words are spread
    over the time the line is actually *sung* (not the whole gap to the
    next line — a line followed by a 20-second guitar solo isn't sung for
    20 seconds), weighted by syllable count so "a" goes by faster than
    "beautiful".
  * LyricTimeline.locate(t) — a Cursor saying which line is on screen,
    which word is being sung and how far through it, or how far through an
    instrumental gap we are (for countdown dots). O(log n) per call.
  * TempoMap — player position -> lyric time. This is what makes a slowed
    or sped-up upload line up: lyrics are timed for the original, so a
    0.8x slowed track's position is multiplied by 0.8 before lookup. See
    variant_scale() for how that factor is chosen.
"""
from __future__ import annotations

import bisect
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .lyrics import LyricLine, Word, is_syllabic_char

_VOWEL_GROUP = re.compile(r"[aeiouyæøœ]+")
_PAUSE_PUNCT = ",;:.!?…—–"


# --------------------------------------------------------------------------
# Word timing estimation
# --------------------------------------------------------------------------

def estimate_syllables(word: str) -> float:
    letters = [c for c in word if c.isalnum()]
    if not letters:
        return 0.3
    syllabic = sum(1 for c in letters if is_syllabic_char(c))
    if syllabic:
        return float(syllabic + (len(letters) - syllabic) / 3.0)
    folded = "".join(c for c in unicodedata.normalize("NFKD", word.lower()) if not unicodedata.combining(c))
    groups = len(_VOWEL_GROUP.findall(folded))
    if groups > 1 and folded.endswith("e") and not folded.endswith(("le", "ee")):
        groups -= 1  # silent final e ("time", "love") — a mild English bias
    if groups == 0:
        return max(1.0, len(letters) / 4.0)  # "hmm", "shh", numbers, non-Latin scripts
    return float(groups)


def split_words(text: str) -> List[Tuple[str, bool]]:
    """(word, space_after) tokens for display and estimation. CJK runs are
    split per character (each character is a sung syllable) without adding
    spaces between them; punctuation stays attached to its character."""
    out: List[Tuple[str, bool]] = []
    for token in text.split():
        if not any(is_syllabic_char(c) for c in token):
            out.append((token, True))
            continue
        pieces: List[str] = []
        for ch in token:
            if is_syllabic_char(ch) or not pieces:
                pieces.append(ch)
            elif pieces and not is_syllabic_char(ch) and not ch.isalnum():
                pieces[-1] += ch      # trailing punctuation
            elif pieces and not is_syllabic_char(pieces[-1][-1]):
                pieces[-1] += ch      # continue a Latin run inside mixed text
            else:
                pieces.append(ch)
        for k, p in enumerate(pieces):
            out.append((p, k == len(pieces) - 1))
    return out


def sung_duration(syllables: float, gap: Optional[float]) -> float:
    """How long a line with this many syllables plausibly takes to sing,
    given the time until the next line (None = last line)."""
    natural = syllables / 2.2 + 0.6          # ~2-5 syllables/s is normal singing
    if gap is None:
        return max(0.8, natural)
    if gap <= 0:
        return 0.3
    return max(min(gap * 0.92, natural), min(gap, 0.3))


def estimate_words(text: str, start: float, next_start: Optional[float],
                   end: Optional[float] = None) -> Tuple[Word, ...]:
    """Estimated word timings for a line. `end`, if given, is when the line
    is known to finish (e.g. from real timing of a differently-worded
    version of the same line)."""
    tokens = split_words(text)
    if not tokens:
        return ()
    weights = []
    for tok, _ in tokens:
        w = estimate_syllables(tok) + 0.35
        if tok[-1] in _PAUSE_PUNCT:
            w += 0.4
        weights.append(w)
    total = sum(weights)
    if end is None:
        gap = (next_start - start) if next_start is not None else None
        end = start + sung_duration(sum(estimate_syllables(t) for t, _ in tokens), gap)
    span = max(end - start, 0.05)
    words: List[Word] = []
    acc = start
    for (tok, space), w in zip(tokens, weights):
        dur = span * w / total
        words.append(Word(text=tok, start=acc, end=acc + dur, space_after=space))
        acc += dur
    return tuple(words)


def _finalize_real_words(words: Sequence[Word], next_start: Optional[float]) -> Tuple[Word, ...]:
    out: List[Word] = []
    for i, w in enumerate(words):
        end = w.end
        nxt = words[i + 1].start if i + 1 < len(words) else None
        if end is None:
            if nxt is not None:
                end = nxt
            else:
                end = w.start + min(1.5, 0.3 + 0.07 * len(w.text))
        if next_start is not None:
            end = min(end, max(next_start, w.start + 0.05))
        end = max(end, w.start + 0.05)
        out.append(Word(w.text, w.start, end, w.space_after))
    return tuple(out)


# --------------------------------------------------------------------------
# Timeline
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Cursor:
    time: float
    index: int            # line being displayed; while in a gap, the last line at/before `time` (-1: none)
    active: bool          # True while a lyric line is on screen, False in intros/gaps/outros
    word_index: int       # current word within timeline.words[index]; -1 before the first word
    word_progress: float  # 0..1 through the current word
    line_progress: float  # 0..1 through the sung part of the line
    since_start: float    # lyric seconds since the current line (or gap) began
    next_index: int       # next non-empty line (-1: none)
    time_to_next: float   # seconds until it starts (inf: none)
    gap_progress: float   # 0..1 through the current gap (only meaningful when not active)
    gap_length: float     # total length of the current gap (inf for the outro)

    @property
    def focus_index(self) -> int:
        """The line a list view should be centred on."""
        if self.active:
            return self.index
        return self.next_index if self.next_index >= 0 else self.index


EMPTY_CURSOR = Cursor(0.0, -1, False, -1, 0.0, 0.0, 0.0, -1, math.inf, 0.0, math.inf)


class LyricTimeline:
    """Immutable index over one song's lyric lines."""

    def __init__(self, lines: Sequence[LyricLine], hold_seconds: Optional[float] = None):
        self.lines: List[LyricLine] = sorted(lines, key=lambda l: l.timestamp)
        self.starts: List[float] = [l.timestamp for l in self.lines]
        n = len(self.lines)
        self._next_text: List[int] = [-1] * n
        nxt = -1
        for i in range(n - 1, -1, -1):
            self._next_text[i] = nxt
            if self.lines[i].text:
                nxt = i
        self._first_text = nxt
        self.hold = hold_seconds if hold_seconds is not None else self._auto_hold()

        self.words: List[Tuple[Word, ...]] = []
        self.word_starts: List[List[float]] = []
        self.sung_end: List[float] = []
        self.clear_at: List[float] = []
        for i, line in enumerate(self.lines):
            next_start = self.starts[i + 1] if i + 1 < n else None
            if not line.text:
                words: Tuple[Word, ...] = ()
            elif line.words:
                words = _finalize_real_words(line.words, next_start)
            else:
                words = estimate_words(line.text, line.timestamp, next_start)
            self.words.append(words)
            self.word_starts.append([w.start for w in words])
            end = words[-1].end if words else line.timestamp
            self.sung_end.append(end if end is not None else line.timestamp)
            limit = next_start if next_start is not None else math.inf
            clear = max(line.timestamp + self.hold, self.sung_end[-1] + 1.0)
            self.clear_at.append(min(limit, clear))

    @property
    def has_word_timing(self) -> bool:
        return any(l.words for l in self.lines)

    @property
    def text_line_count(self) -> int:
        return sum(1 for l in self.lines if l.text)

    def _auto_hold(self) -> float:
        """How long a line stays up with nothing after it: roughly as long
        as this song's own median line gap suggests a line "occupies",
        clamped so an outro clears within a few seconds."""
        gaps = sorted(b - a for a, b in zip(self.starts, self.starts[1:]) if b - a > 0)
        if not gaps:
            return 8.0
        return max(4.0, min(12.0, gaps[len(gaps) // 2] * 1.5))

    def min_line_gap(self) -> Optional[float]:
        gaps = [b - a for a, b in zip(self.starts, self.starts[1:]) if b - a > 0]
        return min(gaps) if gaps else None

    def next_text_index(self, i: int) -> int:
        if i < 0:
            return self._first_text
        return self._next_text[i] if i < len(self._next_text) else -1

    def prev_text_index(self, i: int) -> int:
        for k in range(min(i, len(self.lines)) - 1, -1, -1):
            if self.lines[k].text:
                return k
        return -1

    def locate(self, t: float) -> Cursor:
        if not self.lines:
            return Cursor(t, -1, False, -1, 0.0, 0.0, 0.0, -1, math.inf, 0.0, math.inf)
        i = bisect.bisect_right(self.starts, t) - 1
        nxt = self.next_text_index(i)
        time_to_next = (self.starts[nxt] - t) if nxt >= 0 else math.inf

        if i >= 0 and self.lines[i].text and t < self.clear_at[i]:
            words = self.words[i]
            wi = bisect.bisect_right(self.word_starts[i], t) - 1
            wp = 0.0
            if wi >= 0:
                w = words[wi]
                wp = min(1.0, max(0.0, (t - w.start) / max(w.end - w.start, 1e-3)))
            start = self.starts[i]
            sung = max(self.sung_end[i] - start, 1e-3)
            lp = min(1.0, max(0.0, (t - start) / sung))
            return Cursor(t, i, True, wi, wp, lp, t - start, nxt, time_to_next, 0.0, 0.0)

        if i < 0:
            gap_start = 0.0
        elif self.lines[i].text:
            gap_start = self.clear_at[i]
        else:
            gap_start = self.starts[i]
        gap_len = (self.starts[nxt] - gap_start) if nxt >= 0 else math.inf
        gp = 0.0
        if nxt >= 0 and gap_len > 0:
            gp = min(1.0, max(0.0, (t - gap_start) / gap_len))
        return Cursor(t, i, False, -1, 0.0, 0.0, t - gap_start, nxt, time_to_next, gp, gap_len)


# --------------------------------------------------------------------------
# Tempo
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TempoMap:
    """lyric_time = player_position * scale + offset.

    scale < 1 for slowed tracks, > 1 for sped-up ones. offset > 0 shows
    lyrics earlier."""
    scale: float = 1.0
    offset: float = 0.0

    def to_lyric(self, position: float) -> float:
        return position * self.scale + self.offset

    def to_player(self, lyric_time: float) -> float:
        return (lyric_time - self.offset) / self.scale if self.scale else lyric_time


# Typical edit speeds, used only when neither durations nor the title give
# a number. Real "slowed + reverb" edits cluster around 0.8-0.85x; sped-up
# and nightcore edits around 1.2-1.3x.
DEFAULT_SLOWED_SCALE = 0.82
DEFAULT_SPED_UP_SCALE = 1.25


@dataclass(frozen=True)
class TempoEstimate:
    scale: float
    source: str   # "none" | "durations" | "title" | "guess" | "exact" | "fingerprint"

    @property
    def is_guess(self) -> bool:
        return self.source == "guess"


def variant_scale(variant: str, speed_hint: Optional[float], player_length: Optional[float],
                  original_length: Optional[float], force: bool = False) -> TempoEstimate:
    """Pick the lyric-time scale for a track.

    Best evidence first:
      1. durations — original recording length / this track's length. A
         slowed edit is a uniform resample, so this ratio *is* the speed.
         Must agree in direction with the variant tag (slowed -> < 1).
      2. an explicit factor in the title ("0.8x").
      3. a typical value for the variant type — flagged as a guess.

    Untagged tracks keep 1.0 unless `force` (the user pressed "match speed
    to track length"): a music video with a long intro also has a length
    mismatch, and stretching its lyrics would be wrong.
    """
    ratio: Optional[float] = None
    if player_length and original_length and player_length > 0:
        ratio = original_length / player_length

    if not variant and not force:
        return TempoEstimate(1.0, "none")

    if ratio is not None and 0.5 <= ratio <= 1.6:
        if abs(ratio - 1.0) <= 0.025:
            # Same length: these lyrics were timed for this exact upload.
            return TempoEstimate(1.0, "exact")
        if force or (variant == "slowed" and ratio < 1.0) or (variant == "sped_up" and ratio > 1.0):
            return TempoEstimate(ratio, "durations")
    if force and not variant:
        return TempoEstimate(1.0, "none")
    if speed_hint:
        return TempoEstimate(speed_hint, "title")
    if variant == "slowed":
        return TempoEstimate(DEFAULT_SLOWED_SCALE, "guess")
    if variant == "sped_up":
        return TempoEstimate(DEFAULT_SPED_UP_SCALE, "guess")
    return TempoEstimate(1.0, "none")
