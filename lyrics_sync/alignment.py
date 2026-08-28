"""Transcript alignment: repair/fill LRC timestamps using a YouTube
transcript when the scraped LRC is missing, wrong, or has repeated/
out-of-order lines.

Guarantees:
  * forward-only — a later lyric can never match an earlier transcript line
    than an earlier lyric already matched
  * a transcript line is consumed at most once
  * duplicate lyric lines (choruses) resolve to *different* transcript
    occurrences, never collapsed onto one timestamp
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import List, Optional, Sequence, Set, Tuple

from .config import AlignmentConfig
from .lyrics import LyricLine
from .transcript import TranscriptLine

_WORD_RE = re.compile(r"[a-z0-9']+", re.I)


def _fold_accents(text: str) -> str:
    """Strip diacritics for matching purposes only (never for display) —
    "cancion" and "canción", or "esta" and "está", should score as equal
    when comparing an LRC line against a transcript line. Spanish (and
    French/Portuguese/etc.) lyric providers are inconsistent about accent
    usage, so scoring on accented text as-is silently drops otherwise-good
    matches to a lower score than they deserve."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", _fold_accents(text.lower())).strip()


def _tokens(text: str) -> Set[str]:
    return set(_WORD_RE.findall(_fold_accents(text.lower())))


def _token_overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


@dataclass(frozen=True)
class AlignedLine:
    text: str
    timestamp: float
    confidence: float
    source: str  # "lrc" | "transcript" | "interpolated"


class ForwardAligner:
    def __init__(self, config: AlignmentConfig):
        self.cfg = config

    def align(
        self,
        lyric_lines: Sequence[LyricLine],
        transcript: Sequence[TranscriptLine],
    ) -> List[AlignedLine]:
        if not transcript:
            return [AlignedLine(l.text, l.timestamp, 1.0, "lrc") for l in lyric_lines]

        results: List[AlignedLine] = []
        cursor = 0                     # transcript index — never moves backward
        last_ts: Optional[float] = None

        for lyric in lyric_lines:
            match_idx, score = self._best_match(lyric.text, transcript, cursor, last_ts)

            if match_idx is not None and score >= self.cfg.min_ratio:
                t = transcript[match_idx]
                results.append(AlignedLine(lyric.text, t.start, score, "transcript"))
                cursor = match_idx + 1     # consumed — never reused
                last_ts = t.start
            elif last_ts is None or lyric.timestamp >= last_ts:
                results.append(AlignedLine(lyric.text, lyric.timestamp, 0.4, "lrc"))
                last_ts = lyric.timestamp
            else:
                interpolated = last_ts + 0.5
                results.append(AlignedLine(lyric.text, interpolated, 0.1, "interpolated"))
                last_ts = interpolated

        return results

    def _best_match(
        self,
        lyric_text: str,
        transcript: Sequence[TranscriptLine],
        cursor: int,
        last_ts: Optional[float],
    ) -> Tuple[Optional[int], float]:
        best_idx, best_score = None, 0.0
        window_end = last_ts + self.cfg.window_seconds if last_ts is not None else None

        for idx in range(cursor, len(transcript)):
            t = transcript[idx]
            if window_end is not None and t.start > window_end and best_idx is not None:
                break  # already have a candidate inside the window — stop expanding

            overlap = _token_overlap(lyric_text, t.text)
            if overlap < self.cfg.min_token_overlap:
                continue
            score = 0.5 * overlap + 0.5 * _ratio(lyric_text, t.text)
            if score > best_score:
                best_idx, best_score = idx, score

        return best_idx, best_score
