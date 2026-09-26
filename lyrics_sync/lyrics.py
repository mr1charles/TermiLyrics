"""Lyrics acquisition: cache-first LRC fetch with pluggable providers.

Search order for a song that isn't cached yet:

  1. LrcLib, queried directly. Every candidate is scored on title, artist
     *and duration* — duration is what tells the album cut apart from the
     radio edit, the live version and the 10-minute music video, and it's
     also what lets a slowed/sped-up upload be re-timed (timing.py).
  2. syncedlyrics (Musixmatch, NetEase, Megalobiz, Genius, ...) with a chain
     of progressively cleaner queries. Synced lyrics only — an unsynced
     result used to end the search early and then parse to nothing.
  3. The optional local Ollama model, to recover the real artist/title of
     heavily retitled uploads, followed by one more search.

Word-by-word timing (for the karaoke modes) is fetched separately, in the
background, after line-synced lyrics are already on screen — see
LyricsService.fetch_word_timing.

Preserves the original Caelestia-mirror behavior so `mysong-fix`/
`mysong-retry` and the Caelestia lyric widget keep working unchanged.
"""
from __future__ import annotations

import bisect
import json
import logging
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .cache import TextCache
from .detect import Song, strip_noise_tags
from .ai import OllamaSongIdentifier

log = logging.getLogger(__name__)

try:
    import syncedlyrics
    HAVE_SYNCEDLYRICS = True
except ImportError:
    HAVE_SYNCEDLYRICS = False


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Word:
    """One timed word (or, for CJK text, one timed character).

    `end` is None when the source didn't say — timing.LyricTimeline fills
    it in. `space_after` records whether the original text had whitespace
    after this word, so CJK lines split per character re-join without
    spurious spaces."""
    text: str
    start: float
    end: Optional[float] = None
    space_after: bool = True


@dataclass(frozen=True)
class LyricLine:
    timestamp: float
    text: str
    # Real word-level timing from an enhanced LRC, when the provider had it.
    # Empty for ordinary line-synced lyrics (timing.py estimates instead).
    words: Tuple[Word, ...] = ()


@dataclass(frozen=True)
class LrcDocument:
    lines: List[LyricLine]
    length: Optional[float] = None   # from a [length:] tag, if present
    title: str = ""
    artist: str = ""

    @property
    def has_word_timing(self) -> bool:
        return any(l.words for l in self.lines)


@dataclass
class LyricsResult:
    lines: List[LyricLine] = field(default_factory=list)
    source: str = ""
    # Length of the recording these timestamps belong to (seconds), when a
    # provider reported it. This is the reference for re-timing variants.
    duration: Optional[float] = None
    instrumental: bool = False
    matched_title: str = ""
    matched_artist: str = ""

    @property
    def has_word_timing(self) -> bool:
        return any(l.words for l in self.lines)

    def __bool__(self) -> bool:
        return bool(self.lines)


# --------------------------------------------------------------------------
# LRC parsing
# --------------------------------------------------------------------------

# [mm:ss], [mm:ss.x], [mm:ss.xx], [mm:ss.xxx] and the [mm:ss:xx] variant.
_TIME_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_WORD_TAG = re.compile(r"<(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)>")
_META_TAG = re.compile(r"^\[([A-Za-z#]+)\s*:(.*)\]\s*$")

# Credit/metadata lines some providers (NetEase in particular) emit as
# timed lyric lines. Never sung — showing "作词 : ..." as the first lyric,
# or an LRC-maker's URL, is just noise.
_CREDIT_RE = re.compile(
    r"^\s*(作词|作曲|编曲|制作人|监制|混音|母带|和声|吉他|贝斯|鼓|录音|出品|发行|词|曲|"
    r"lyrics?\s*by|written\s*by|composed\s*by|producers?|produced\s*by|arranged\s*by|"
    r"mixed\s*by|mastered\s*by)\s*[:：]", re.I)
_SPAM_RE = re.compile(r"https?://|www\.|rentanadviser|\blrc\s+(by|maker|editor)\b", re.I)

_CJK_RANGES = (
    (0x3040, 0x30FF),   # hiragana + katakana
    (0x3400, 0x4DBF),   # CJK ext A
    (0x4E00, 0x9FFF),   # CJK unified
    (0xAC00, 0xD7A3),   # hangul syllables
    (0xF900, 0xFAFF),   # CJK compatibility
)


def is_syllabic_char(ch: str) -> bool:
    """CJK/kana/hangul: one character is one sung syllable, and words
    aren't separated by spaces."""
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _parse_time(minutes: str, seconds: str) -> float:
    return int(minutes) * 60 + float(seconds.replace(":", "."))


def _normalize_space(text: str) -> str:
    return " ".join(text.split())


def _is_credit_line(text: str) -> bool:
    return bool(_CREDIT_RE.match(text) or _SPAM_RE.search(text))


def _parse_words(content: str, offset: float) -> Tuple[Word, ...]:
    """Word timings from an enhanced-LRC line body.

    Handles both common shapes:
      A2 / "enhanced" LRC     <00:12.00>Hello <00:12.50>world<00:13.10>
      syncedlyrics richsync   <00:12.00> Hello <00:12.50>   <00:12.60> world
    A timed whitespace-only (or empty, trailing) token marks the end of the
    word before it. Boundaries between CJK characters count as word breaks
    even without a space, so a per-character timed CJK line keeps its
    per-character timing."""
    parts = _WORD_TAG.split(content)
    if len(parts) < 4:
        return ()
    tokens: List[Tuple[float, str]] = []
    for i in range(1, len(parts) - 2, 3):
        try:
            t = _parse_time(parts[i], parts[i + 1]) - offset
        except ValueError:
            continue
        tokens.append((max(0.0, t), parts[i + 2]))
    if not tokens:
        return ()
    if parts[0].strip():  # untimed text before the first tag belongs to it
        tokens[0] = (tokens[0][0], parts[0] + tokens[0][1])

    # Each entry: [text, start, end, space_after]
    words: List[list] = []
    cur: Optional[list] = None

    def close_word(space: bool) -> None:
        nonlocal cur
        if cur is not None:
            cur[3] = space
            words.append(cur)
            cur = None

    for t, text in tokens:
        if not text.strip():
            # A timed gap: whatever word is open (or was just closed) ends here.
            if cur is not None:
                close_word(True)
            if words and words[-1][2] is None and t > words[-1][1]:
                words[-1][2] = t
            continue
        for ch in text:
            if ch.isspace():
                close_word(True)
                continue
            if cur is not None and (is_syllabic_char(ch) or is_syllabic_char(cur[0][-1])):
                close_word(False)
            if cur is None:
                cur = [ch, t, None, True]
            else:
                cur[0] += ch
        # A token boundary between CJK characters is a word boundary too.
        if cur is not None and is_syllabic_char(cur[0][-1]):
            close_word(False)
    close_word(True)

    # Several words sharing one start time (a multi-word token) get spread
    # evenly, by length, up to the next distinct time.
    result: List[Word] = []
    i = 0
    while i < len(words):
        j = i
        while j + 1 < len(words) and words[j + 1][1] == words[i][1]:
            j += 1
        if j > i:
            nxt = words[j + 1][1] if j + 1 < len(words) else words[j][2]
            span_start = words[i][1]
            if nxt is None or nxt <= span_start:
                nxt = span_start + 0.3 * (j - i + 1)
            total = sum(len(w[0]) for w in words[i:j + 1]) or 1
            acc = span_start
            for k in range(i, j + 1):
                dur = (nxt - span_start) * len(words[k][0]) / total
                words[k][1] = acc
                if k < j:
                    words[k][2] = acc + dur
                acc += dur
        i = j + 1
    for text, start, end, space in words:
        if end is not None and end <= start:
            end = None
        result.append(Word(text=text, start=start, end=end, space_after=space))
    return tuple(result)


def _text_from_words(words: Sequence[Word]) -> str:
    return "".join(w.text + (" " if w.space_after else "") for w in words).strip()


def parse_lrc_document(text: str) -> LrcDocument:
    """Parse LRC text, including the parts of the format real-world files
    actually use:

      * several timestamps on one line ([00:12.00][01:30.00]chorus) — the
        line is repeated at each, instead of the rest of the tags being
        shown as lyric text
      * the [offset:±ms] tag (positive = lyrics earlier)
      * [length:], [ti:] and [ar:] metadata
      * enhanced/word-level timing (<mm:ss.xx>word)
      * credit and LRC-maker lines, which are dropped
    """
    meta: Dict[str, str] = {}
    entries: List[Tuple[float, str]] = []
    for raw in (text or "").splitlines():
        raw = raw.strip().lstrip("﻿")
        if not raw:
            continue
        stamps: List[float] = []
        pos = 0
        while True:
            m = _TIME_TAG.match(raw, pos)
            if not m:
                break
            try:
                stamps.append(_parse_time(m.group(1), m.group(2)))
            except ValueError:
                pass
            pos = m.end()
            while pos < len(raw) and raw[pos] == " " and raw.startswith("[", pos + 1):
                pos += 1
        if not stamps:
            mm = _META_TAG.match(raw)
            if mm:
                meta[mm.group(1).strip().lower()] = mm.group(2).strip()
            continue
        content = raw[pos:]
        for ts in stamps:
            entries.append((ts, content))

    offset = 0.0
    if "offset" in meta:
        try:
            offset = float(meta["offset"]) / 1000.0
        except ValueError:
            offset = 0.0

    lines: List[LyricLine] = []
    for ts, content in entries:
        t = max(0.0, ts - offset)
        words = _parse_words(content, offset)
        plain = _normalize_space(_WORD_TAG.sub(" " if words else "", content))
        if words:
            plain = _text_from_words(words) or plain
        if plain and _is_credit_line(plain):
            continue
        lines.append(LyricLine(timestamp=t, text=plain, words=words))
    lines.sort(key=lambda l: l.timestamp)

    length: Optional[float] = None
    if "length" in meta:
        m = re.match(r"^\s*(\d+):(\d{1,2}(?:\.\d+)?)\s*$", meta["length"])
        try:
            length = _parse_time(m.group(1), m.group(2)) if m else float(meta["length"])
        except ValueError:
            length = None
    return LrcDocument(lines=lines, length=length, title=meta.get("ti", ""), artist=meta.get("ar", ""))


def parse_lrc(text: str) -> List[LyricLine]:
    return parse_lrc_document(text).lines


# --------------------------------------------------------------------------
# Fuzzy matching helpers
# --------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_PAREN_RE = re.compile(r"\s*[\(\[].*?[\)\]]")
_VERSION_WORDS = {
    "live": 0.2, "remix": 0.2, "acoustic": 0.15, "instrumental": 0.5, "karaoke": 0.5,
    "slowed": 0.25, "sped up": 0.25, "nightcore": 0.25, "reverb": 0.1, "cover": 0.2,
    "demo": 0.1, "edit": 0.1, "extended": 0.15, "version": 0.05, "mix": 0.1,
}


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize_title(text: str) -> str:
    """Comparison form: noise tags/feat stripped, accents folded,
    punctuation removed, lowercase."""
    t = _fold(strip_noise_tags(text or "").lower())
    t = _PUNCT_RE.sub(" ", t.replace("&", " and "))
    return " ".join(t.split())


def similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    # "Blinding Lights" vs "Blinding Lights (From the Movie)": containment is
    # a strong signal the plain ratio undersells.
    short, long_ = sorted((na, nb), key=len)
    if f" {short} " in f" {long_} " and len(short) >= 0.4 * len(long_):
        ratio = max(ratio, 0.88)
    return ratio


def _version_penalty(candidate_title: str, requested_title: str) -> float:
    cand, req = candidate_title.lower(), requested_title.lower()
    return sum(p for w, p in _VERSION_WORDS.items()
               if re.search(rf"\b{w}\b", cand) and not re.search(rf"\b{w}\b", req))


def _duration_score(candidate: Optional[float], expected: Optional[float]) -> Optional[float]:
    if not candidate or not expected:
        return None
    diff = abs(candidate - expected)
    if diff <= 2.0:
        return 1.0
    if diff <= 5.0:
        return 0.75
    if diff <= 10.0:
        return 0.45
    if diff <= 25.0:
        return 0.15
    return 0.0


# --------------------------------------------------------------------------
# HTTP providers
# --------------------------------------------------------------------------

HttpGet = Callable[[str, Dict[str, Any]], Any]


def _default_http_get(user_agent: str, timeout: float) -> HttpGet:
    def get(url: str, params: Dict[str, Any]) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        req = urllib.request.Request(f"{url}?{query}" if query else url,
                                     headers={"User-Agent": user_agent, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
    return get


class LrcLibClient:
    """Minimal client for https://lrclib.net's public API (no key needed)."""

    BASE = "https://lrclib.net/api"

    def __init__(self, http_get: HttpGet):
        self._get = http_get

    def search(self, track: str = "", artist: str = "", q: str = "") -> List[Dict[str, Any]]:
        params = {"q": q} if q else {"track_name": track, "artist_name": artist}
        data = self._get(f"{self.BASE}/search", params)
        return [c for c in (data or []) if isinstance(c, dict)]


@dataclass
class _Scored:
    candidate: Dict[str, Any]
    score: float
    title_sim: float


def score_candidates(candidates: Sequence[Dict[str, Any]], song: Song,
                     expected_duration: Optional[float],
                     require_synced: bool = True) -> List[_Scored]:
    """Rank LrcLib candidates for `song`, best first."""
    use_artist = song.artist_confidence != "low" and bool(song.artist.strip())
    scored: List[_Scored] = []
    for c in candidates:
        synced = (c.get("syncedLyrics") or "").strip()
        if require_synced and not synced and not c.get("instrumental"):
            continue
        title = c.get("trackName") or c.get("name") or ""
        t_sim = similarity(title, song.title)
        parts = [(0.55, t_sim)]
        if use_artist:
            parts.append((0.30, similarity(c.get("artistName") or "", song.artist)))
        elif song.artist.strip():
            # Low-confidence artist (maybe an uploader name): a match is
            # still mild evidence, a mismatch isn't evidence of anything.
            a_sim = similarity(c.get("artistName") or "", song.artist)
            if a_sim > 0.7:
                parts.append((0.15, a_sim))
        d = _duration_score(c.get("duration"), expected_duration)
        if d is not None:
            parts.append((0.20, d))
        total = sum(w * s for w, s in parts) / sum(w for w, _ in parts)
        total -= _version_penalty(title, song.title)
        scored.append(_Scored(c, total, t_sim))
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------

_MISSING_TTL_SECONDS = 12 * 3600       # don't re-scrape a known-missing song for this long
_WORDS_RECHECK_SECONDS = 3 * 24 * 3600  # ...or re-ask Musixmatch for word timing


class LyricsService:
    def __init__(self, cache: TextCache, providers: Sequence[str],
                 caelestia_dir: Optional[Path] = None,
                 song_identifier: Optional[OllamaSongIdentifier] = None,
                 http_get: Optional[HttpGet] = None,
                 lrclib_enabled: bool = True,
                 musicbrainz_enabled: bool = True,
                 user_agent: str = "TermiLyrics",
                 http_timeout: float = 8.0):
        self.cache = cache
        self.providers = list(providers)
        self.caelestia_dir = caelestia_dir
        # Genuine last resort — see _scrape. Optional; None means this
        # fallback tier is simply skipped.
        self.song_identifier = song_identifier
        self._http_get = http_get or _default_http_get(user_agent, http_timeout)
        self.lrclib = LrcLibClient(self._http_get) if lrclib_enabled else None
        self.musicbrainz_enabled = musicbrainz_enabled
        # syncedlyrics' Musixmatch client retries a rejected token forever
        # (sleep 10s, recurse). Never let more than one word-timing lookup
        # be stuck in that at a time.
        self._words_lock = threading.Lock()

    # ---- metadata sidecar (duration, source, negative cache) ----

    def _read_meta(self, key: str) -> Dict[str, Any]:
        raw = self.cache.read(key, "json")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            self.cache.evict(key, "json")
            return {}

    def _write_meta(self, key: str, **updates: Any) -> None:
        meta = self._read_meta(key)
        meta.update(updates)
        self.cache.write(key, json.dumps(meta, ensure_ascii=False), "json")

    # ---- public API ----

    def fetch(self, song: Song, force_retry: bool = False,
              player_length: Optional[float] = None) -> LyricsResult:
        key = song.key
        if force_retry:
            for ext in ("lrc", "elrc", "json"):
                self.cache.evict(key, ext)

        meta = self._read_meta(key)
        if not force_retry:
            cached = self._from_cache(key, meta)
            if cached is not None:
                return cached
            missing_at = meta.get("missing")
            if isinstance(missing_at, (int, float)) and time.time() - missing_at < _MISSING_TTL_SECONDS:
                return LyricsResult(source="cache", instrumental=bool(meta.get("instrumental")))

        expected = self._expected_original_length(song, player_length)
        result: Optional[LyricsResult] = None
        lrclib_answered = False
        if self.lrclib is not None:
            try:
                result = self._from_lrclib(song, player_length, expected)
                lrclib_answered = True
            except Exception as e:  # network/provider failure: fall through to syncedlyrics
                log.info("LrcLib lookup failed for %r: %s", song, e)

        if (result is None or not result.lines) and not (result is not None and result.instrumental):
            text = self._scrape(song, expected) if HAVE_SYNCEDLYRICS else ""
            if text:
                doc = parse_lrc_document(text)
                if doc.lines:
                    result = LyricsResult(lines=doc.lines, source="syncedlyrics", duration=doc.length)
                    self.cache.write(key, text)
                    self._write_meta(key, source="syncedlyrics", duration=doc.length, missing=None,
                                     fetched=time.time())
                    self._sync_caelestia(song, text)

        if result is None:
            result = LyricsResult()
        if not result.lines and lrclib_answered:
            self._write_meta(key, missing=time.time(), instrumental=result.instrumental)
        return result

    def fetch_lines(self, song: Song, force_retry: bool = False) -> List[LyricLine]:
        """Backwards-compatible: just the lines."""
        return self.fetch(song, force_retry).lines

    def fetch_word_timing(self, song: Song, base: Sequence[LyricLine]) -> Optional[List[LyricLine]]:
        """Word-by-word timing for a song whose line-synced lyrics are
        already showing. Returns None when unavailable or when what the
        provider has doesn't match `base` (a different version/edit would
        make every highlighted word wrong). Blocking — run off-loop."""
        key = song.key
        cached = self.cache.read(key, "elrc")
        if cached:
            doc = parse_lrc_document(cached)
            if doc.has_word_timing:
                return doc.lines
        if not HAVE_SYNCEDLYRICS:
            return None
        meta = self._read_meta(key)
        checked = meta.get("words_checked")
        if isinstance(checked, (int, float)) and time.time() - checked < _WORDS_RECHECK_SECONDS:
            return None

        use_artist = song.artist_confidence != "low" and song.artist.strip()
        query = f"{song.title} {song.artist}".strip() if use_artist else song.title
        if not self._words_lock.acquire(blocking=False):
            return None  # a previous lookup is still stuck; try again next song
        try:
            text = syncedlyrics.search(query, providers=["Musixmatch"], enhanced=True, synced_only=True) or ""
        except Exception as e:
            log.debug("word-level lookup failed for %r: %s", query, e)
            return None  # transient — don't mark as checked
        finally:
            self._words_lock.release()
        doc = parse_lrc_document(text) if text else None
        if not doc or not doc.has_word_timing or not lyrics_match(base, doc.lines):
            self._write_meta(key, words_checked=time.time())
            return None
        self.cache.write(key, text, "elrc")
        return doc.lines

    def lookup_duration(self, song: Song) -> Optional[float]:
        """Length of the *original* recording, for re-timing a slowed or
        sped-up upload when the lyrics provider didn't report one."""
        meta = self._read_meta(song.key)
        if isinstance(meta.get("duration"), (int, float)) and meta["duration"] > 0:
            return float(meta["duration"])
        duration: Optional[float] = None
        if self.lrclib is not None:
            try:
                cands = self.lrclib.search(track=song.title, artist=song.artist if song.artist_confidence != "low" else "")
                ranked = score_candidates(cands, song, None, require_synced=False)
                if ranked and ranked[0].title_sim >= 0.8 and ranked[0].candidate.get("duration"):
                    duration = float(ranked[0].candidate["duration"])
            except Exception as e:
                log.debug("LrcLib duration lookup failed: %s", e)
        if duration is None and self.musicbrainz_enabled:
            duration = self._musicbrainz_duration(song)
        if duration:
            self._write_meta(song.key, duration=duration)
        return duration

    # ---- internals ----

    def _from_cache(self, key: str, meta: Dict[str, Any]) -> Optional[LyricsResult]:
        text = self.cache.read(key)
        if text is None:
            return None
        doc = parse_lrc_document(text)
        if not doc.lines:
            return None
        lines = doc.lines
        words_text = self.cache.read(key, "elrc")
        if words_text:
            wdoc = parse_lrc_document(words_text)
            if wdoc.has_word_timing:
                lines = wdoc.lines
        duration = meta.get("duration") if isinstance(meta.get("duration"), (int, float)) else doc.length
        return LyricsResult(lines=lines, source=f"cache:{meta.get('source', 'unknown')}", duration=duration,
                            matched_title=meta.get("title", ""), matched_artist=meta.get("artist", ""))

    @staticmethod
    def _expected_original_length(song: Song, player_length: Optional[float]) -> Optional[float]:
        """What the original recording's duration should be, judging from
        the track actually playing. For a variant only an explicit speed
        factor lets us say; otherwise we don't know."""
        if not player_length or player_length <= 0:
            return None
        if not song.variant:
            return player_length
        if song.speed_hint:
            return player_length * song.speed_hint
        return None

    def _lrclib_queries(self, song: Song) -> List[Dict[str, str]]:
        use_artist = song.artist_confidence != "low" and song.artist.strip()
        title = song.title.strip()
        bare = _PAREN_RE.sub("", title).strip()
        queries: List[Dict[str, str]] = []
        if use_artist:
            queries.append({"track": title, "artist": song.artist})
            queries.append({"q": f"{song.artist} {title}"})
        else:
            queries.append({"q": f"{song.artist} {title}".strip()} if song.artist.strip() else {"q": title})
            queries.append({"track": title})
        if bare and bare != title:
            queries.append({"track": bare, "artist": song.artist if use_artist else ""})
        return queries

    def _from_lrclib(self, song: Song, player_length: Optional[float],
                     expected: Optional[float]) -> Optional[LyricsResult]:
        assert self.lrclib is not None
        seen: Dict[Any, Dict[str, Any]] = {}
        best: Optional[_Scored] = None
        for q in self._lrclib_queries(song):
            for c in self.lrclib.search(**q):
                seen.setdefault(c.get("id", id(c)), c)
            ranked = score_candidates(list(seen.values()), song, expected)
            if song.variant and player_length:
                ranked = self._prefer_exact_variant(ranked, song, player_length)
            if ranked and ranked[0].title_sim >= 0.55 and ranked[0].score >= 0.5:
                best = ranked[0]
                if best.score >= 0.85:
                    break
        if best is None:
            return None
        c = best.candidate
        synced = (c.get("syncedLyrics") or "").strip()
        duration = float(c["duration"]) if c.get("duration") else None
        if not synced:
            return LyricsResult(source="lrclib", duration=duration, instrumental=bool(c.get("instrumental")),
                                matched_title=c.get("trackName", ""), matched_artist=c.get("artistName", ""))
        doc = parse_lrc_document(synced)
        if not doc.lines:
            return None
        self.cache.write(song.key, synced)
        self._write_meta(song.key, source="lrclib", duration=duration, missing=None, fetched=time.time(),
                         title=c.get("trackName", ""), artist=c.get("artistName", ""), lrclib_id=c.get("id"))
        self._sync_caelestia(song, synced)
        return LyricsResult(lines=doc.lines, source="lrclib", duration=duration,
                            matched_title=c.get("trackName", ""), matched_artist=c.get("artistName", ""))

    @staticmethod
    def _prefer_exact_variant(ranked: List[_Scored], song: Song, player_length: float) -> List[_Scored]:
        """For a slowed/sped-up track, an LRC made *for that exact upload*
        (same variant tag, same length) beats re-timing the original. The
        variant-word penalty would otherwise rank it below the original."""
        exact = [s for s in ranked
                 if s.candidate.get("duration")
                 and abs(float(s.candidate["duration"]) - player_length) <= max(2.0, 0.02 * player_length)
                 and re.search(r"slowed|sped|nightcore|daycore", (s.candidate.get("trackName") or ""), re.I)
                 and s.title_sim >= 0.55]
        if not exact:
            return ranked
        for s in exact:
            s.score = max(s.score, 0.9)
        return exact + [s for s in ranked if s not in exact]

    def _scrape(self, song: Song, expected: Optional[float] = None) -> str:
        providers = [p for p in self.providers if not (self.lrclib is not None and p.lower() == "lrclib")]
        if not providers:
            providers = list(self.providers)
        first_found = ""
        for query in self._query_chain(song):
            try:
                text = syncedlyrics.search(query, providers=providers, synced_only=True) or ""
            except Exception as e:  # provider errors are numerous and non-fatal
                log.debug("lyric scrape failed for %r: %s", query, e)
                continue
            if not text:
                continue
            if _plausible_for_length(text, expected):
                return text
            log.info("rejecting lyrics for %r: longer than the track (%.0fs)", query, expected or 0)
            first_found = first_found or text

        # Every direct query came up empty. For a heavily-retitled track
        # even a noise-stripped title can still not match anything. Genuine
        # last resort: ask the local Ollama model (if configured — see
        # ai.py's OllamaSongIdentifier) to recognize the real song, and try
        # once more with its answer. Only runs after everything else failed,
        # so it costs nothing on the common successful path.
        if self.song_identifier is not None:
            refined = self.song_identifier.identify(song.title, song.artist)
            if refined:
                ai_artist, ai_title = refined
                query = f"{ai_title} {ai_artist}".strip()
                try:
                    text = syncedlyrics.search(query, providers=providers, synced_only=True) or ""
                except Exception as e:
                    log.debug("AI-assisted lyric scrape failed for %r: %s", query, e)
                    text = ""
                if text:
                    return text

        # Lyrics that run past the end of the track are almost certainly a
        # different version — but their first half usually still lines up,
        # which beats showing nothing.
        return first_found

    def _query_chain(self, song: Song) -> List[str]:
        """Ordered list of query strings to try, most-specific first,
        falling back to progressively cleaner variants. For a slowed/sped-up
        upload the clean (original) title goes first: the original song's
        LRC is what we know how to re-time."""
        raw_title = song.title.strip()
        cleaned_title = strip_noise_tags(raw_title)
        bare_title = _PAREN_RE.sub("", cleaned_title).strip()
        has_artist = song.artist_confidence != "low" and song.artist.strip()

        candidates = []
        titles = [raw_title, cleaned_title, bare_title]
        if song.variant:
            titles = [cleaned_title, bare_title, raw_title]
        for t in titles:
            if not t:
                continue
            if has_artist:
                candidates.append(f"{t} {song.artist}".strip())
            candidates.append(t)

        seen: set = set()
        chain: List[str] = []
        for q in candidates:
            if q and q not in seen:
                seen.add(q)
                chain.append(q)
        return chain

    def _musicbrainz_duration(self, song: Song) -> Optional[float]:
        title = song.title.replace('"', "")
        query = f'recording:"{title}"'
        if song.artist.strip() and song.artist_confidence != "low":
            query += f' AND artist:"{song.artist.replace(chr(34), "")}"'
        try:
            data = self._http_get("https://musicbrainz.org/ws/2/recording/",
                                  {"query": query, "fmt": "json", "limit": 5})
        except Exception as e:
            log.debug("MusicBrainz lookup failed: %s", e)
            return None
        best: Optional[Tuple[int, float]] = None
        for rec in (data or {}).get("recordings", []) or []:
            score, length = int(rec.get("score", 0) or 0), rec.get("length")
            if score >= 85 and length and similarity(rec.get("title", ""), song.title) >= 0.8:
                if best is None or score > best[0]:
                    best = (score, length / 1000.0)
        return best[1] if best else None

    def _sync_caelestia(self, song: Song, lrc_text: str) -> None:
        if not self.caelestia_dir or not self.caelestia_dir.exists():
            return
        try:
            filename = f"{song.artist.lower().strip()} - {song.title.lower().strip()}.lrc"
            (self.caelestia_dir / filename).write_text(lrc_text, encoding="utf-8")
        except OSError as e:
            log.debug("caelestia sync failed: %s", e)


def _plausible_for_length(lrc_text: str, expected: Optional[float]) -> bool:
    """False when the lyrics provably belong to a longer recording."""
    if not expected:
        return True
    lines = [l for l in parse_lrc(lrc_text) if l.text]
    if not lines:
        return True
    return lines[-1].timestamp <= expected + 15.0


def lyrics_match(a: Sequence[LyricLine], b: Sequence[LyricLine]) -> bool:
    """Whether two lyric sets are the same song *and* the same timing
    (within a couple of seconds) — used before swapping in word-level
    timing from a different provider."""
    ta = [l for l in a if l.text]
    tb = [l for l in b if l.text]
    if not ta or not tb:
        return False
    text_a = normalize_title(" ".join(l.text for l in ta))[:800]
    text_b = normalize_title(" ".join(l.text for l in tb))[:800]
    if SequenceMatcher(None, text_a, text_b).ratio() < 0.6:
        return False
    deltas: List[float] = []
    j = 0
    for la in ta[:12]:
        for k in range(j, min(j + 4, len(tb))):
            if similarity(la.text, tb[k].text) >= 0.7:
                deltas.append(abs(la.timestamp - tb[k].timestamp))
                j = k + 1
                break
    if len(deltas) < min(3, len(ta)):
        return False
    deltas.sort()
    return deltas[len(deltas) // 2] <= 2.0


def current_and_next(
    lines: List[LyricLine], position: float, max_hold_seconds: Optional[float] = None
) -> Tuple[str, str]:
    """Returns (current_line_text, next_line_text) for the given playback
    position. `max_hold_seconds`, if given, clears `current` back to "" once
    playback has moved more than that far past the current line's own
    timestamp with no next line yet reached. (The display itself now uses
    timing.LyricTimeline, which also knows when a line has been sung.)"""
    starts = [l.timestamp for l in lines]
    i = bisect.bisect_right(starts, position) - 1
    if i < 0:
        return "", lines[0].text if lines else ""
    current = lines[i].text
    nxt = lines[i + 1].text if i + 1 < len(lines) else ""
    if max_hold_seconds is not None and position - lines[i].timestamp > max_hold_seconds:
        current = ""
    return current, nxt
