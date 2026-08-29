"""Song identification: turn raw player/browser titles into a clean Song.

Different sources embed artist/title differently:
  Spotify / MPV / VLC (via playerctl)   already split into artist + title
  YouTube tab title                     "Artist - Song (Official Video)"
  YouTube Music tab title               "Song - Artist"
  Lyric-video uploads                   "7clouds - Artist - Song (Lyrics)"
  Firefox/Chromium tab titles           "<title> - Mozilla Firefox"

Noise-tag and uploader-keyword lists cover both English and Spanish, since
those are the two languages exercised so far (e.g. "(Letra)", "(En Vivo)",
uploader channels like "zilmusic" or "Taj Tracks").
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_NOISE_TAGS = [
    r"\(official\s*video\)", r"\(official\s*audio\)", r"\(official\s*music\s*video\)",
    r"\(lyrics?\)", r"\(lyric\s*video\)", r"\(audio\)", r"\(visualizer\)",
    r"\[official\s*video\]", r"\[lyrics?\]", r"\bofficial\s*video\b", r"\bofficial\s*audio\b",
    r"\bhd\b", r"\b4k\b", r"\bmv\b",
    # Spanish equivalents
    r"\(video\s*oficial\)", r"\(audio\s*oficial\)", r"\(letra\)", r"\(letras\)",
    r"\(en\s*vivo\)", r"\(oficial\)", r"\[video\s*oficial\]", r"\[letras?\]",
    r"\bvideo\s*oficial\b", r"\baudio\s*oficial\b", r"\ben\s*vivo\b",
    # TikTok/remix-culture edit descriptors — these get appended to a real
    # song's title (often as "- Sped Up") but aren't part of it. Left in,
    # they get treated as if THEY were the title, and searching a lyric
    # provider for a song literally called "Sped Up" matches something
    # completely unrelated.
    r"\bsped\s*up\b", r"\bslowed(\s*\+?\s*reverb)?\b", r"\bnightcore\b",
    r"\b8d\s*audio\b", r"\bbass\s*boosted\b", r"\btiktok\s*version\b",
]
_FEAT_TAGS = [
    r"\(feat\..*?\)", r"\(ft\..*?\)", r"\(with\s.*?\)", r"\[feat\..*?\]",
    r"\(con\s.*?\)",  # Spanish "featuring"
]
_KNOWN_LYRIC_UPLOADERS = {"7clouds", "lyrics", "ilyricsclub", "vevo", "topic"}

# Generalizes _KNOWN_LYRIC_UPLOADERS into a pattern: most lyric/music/genre
# channels follow a small set of naming conventions in either language
# ("R&BHype", "Taj Tracks", "Pizza Music", "zilmusic", "future resonance" is
# the one shape this still can't catch — no keyword to key off of). Matching
# is deliberately loose (substring, not exact) because the cost of a false
# positive here is small: it just means we search lyrics by title alone
# instead of title+artist, which is still a safe, usually-correct fallback.
_UPLOADER_KEYWORDS = (
    "hype", "tracks", "music", "musica", "música", "clouds", "vibes", "tunes",
    "sounds", "beats", "lyrics", "letra", "letras", "radio", "records",
    "studio", "topic", "vevo", "audio", "official", "oficial", "playlist",
    "mix", "waves", "vault", "hub", "zone", "nation", "media", "tv",
)


def _looks_like_uploader(name: str) -> bool:
    compact = re.sub(r"[^a-z0-9ñáéíóúü]", "", name.lower())
    if not compact:
        return False
    if name.strip().lower() in _KNOWN_LYRIC_UPLOADERS:
        return True
    return any(kw in compact for kw in _UPLOADER_KEYWORDS)


def is_advertisement(raw_title: str, raw_artist: str) -> bool:
    """Best-effort detection of an ad break, e.g. Spotify's free-tier ads
    (which typically report artist/title as literally "Spotify"/"Advertisement").
    Deliberately conservative — only matches specific, low-false-positive
    patterns, since a false positive here means genuinely skipping a real
    song's lyrics entirely."""
    t = (raw_title or "").strip().lower()
    a = (raw_artist or "").strip().lower()
    if "advertisement" in t or "advertisement" in a:
        return True
    if a == "spotify" and (not t or t == "spotify" or t == "advertisement"):
        return True
    return False


_BROWSER_CHROME = re.compile(
    r"\s*-\s*(Mozilla Firefox|Google Chrome|Chromium|Brave|Microsoft Edge)\s*$", re.I
)
_TAB_COUNT = re.compile(r"\s*-\s*\d+\s+more\s+tabs?\s*$", re.I)

# YouTube auto-generates a "Topic" channel per artist for uploaded tracks;
# playerctl reports that channel name as the artist field verbatim, e.g.
# "Enjambre - Topic" instead of "Enjambre". Strip it before it poisons the
# lyric-provider search query.
_ARTIST_CHANNEL_SUFFIX = re.compile(r"\s*-\s*topic\s*$", re.I)

_TRAILING_ASCII_PAREN = re.compile(r"\s*\(([\x20-\x7E]+)\)\s*$")


def _is_non_latin_script(text: str) -> bool:
    """True if most of the letters in `text` belong to a non-Latin script
    (Cyrillic, CJK, Arabic, Greek, Hebrew, Thai, ...). Deliberately checks
    the Unicode character *name* rather than just "is this ASCII" — accented
    Latin (Björk, Canción) still counts as Latin here, so a legitimate
    qualifier like "Björk (Unplugged)" is never mistaken for a translation."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    non_latin = sum(1 for c in letters if "LATIN" not in unicodedata.name(c, ""))
    return non_latin / len(letters) > 0.5


def _strip_translation_suffix(text: str) -> str:
    """YouTube shows an auto-translated English title in parentheses next to
    non-English titles, e.g. "Базовый минимум (Bare minimum)". That
    translation isn't part of the real title and will never match what a
    lyric provider has stored — strip it. Only fires when the part *before*
    the parenthetical is itself non-Latin script, so this never touches an
    ordinary qualifier like "(Remix)" or "(Live)" on a Latin-script title."""
    m = _TRAILING_ASCII_PAREN.search(text)
    if not m:
        return text
    before = text[: m.start()].strip()
    if before and _is_non_latin_script(before):
        return before
    return text


@dataclass(frozen=True)
class Song:
    artist: str
    title: str
    # "high": artist came straight from an authoritative player field
    #         (Spotify/MPV/VLC via playerctl).
    # "low":  artist was *guessed* by splitting an ambiguous browser-tab
    #         title — could easily be the uploader/channel name instead of
    #         the real artist. Callers (lyrics.py) should avoid feeding a
    #         low-confidence artist into a provider search query, since a
    #         wrong guess degrades fuzzy matching worse than no artist at
    #         all — it's how "R&BHype - Love Me Not" ends up matching some
    #         unrelated "Love Me or Not" instead of the right song.
    artist_confidence: str = "high"

    @property
    def key(self) -> str:
        return f"{self.artist.strip().lower()}::{self.title.strip().lower()}"


def _strip_noise(text: str) -> str:
    for pat in _NOISE_TAGS + _FEAT_TAGS:
        text = re.sub(pat, "", text, flags=re.I)
    # A tag stripped by its bare word (e.g. "Nightcore" inside "(Nightcore)")
    # leaves an empty bracket pair behind — clean those up too.
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)
    return re.sub(r"\s{2,}", " ", text).strip(" -–|")


# Public alias — lyrics.py's fallback search chain reuses this exact
# cleaner rather than re-implementing noise-tag stripping a second time.
strip_noise_tags = _strip_noise


def _strip_browser_chrome(text: str) -> str:
    return _TAB_COUNT.sub("", _BROWSER_CHROME.sub("", text)).strip()


def _resolve_artist_field(raw_artist: str) -> "tuple[str, str]":
    """The artist field itself can be a compound "Uploader - RealArtist"
    string (some browser-reported YouTube metadata does this, not just the
    title) — e.g. artist="Taj Tracks - Lady Gaga". Split it the same way we
    split compound titles. Returns (best_guess_artist, confidence)."""
    artist = _ARTIST_CHANNEL_SUFFIX.sub("", raw_artist.strip()).strip()
    if not artist:
        return "", "high"

    parts = [p.strip() for p in re.split(r"\s[-–]\s", artist) if p.strip()]
    if len(parts) < 2:
        return artist, "high"
    if _looks_like_uploader(parts[0]):
        return parts[-1], "high"
    # Ambiguous: no reliable way to know which segment is the uploader vs.
    # the real artist. Guess the last segment (uploader-prefix is the more
    # common convention) but flag it low-confidence so lyrics.py won't feed
    # a possibly-wrong artist into the search query.
    return parts[-1], "low"


def _artist_agrees_with_title_guess(metadata_artist: str, title_guess: str) -> bool:
    """Loose check for whether the metadata artist field and the artist
    implied by the title's own "A - B" split are plausibly the same entity."""
    a, b = metadata_artist.strip().lower(), title_guess.strip().lower()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def identify(raw_title: str, raw_artist: str = "", source: str = "") -> Song:
    """Best-effort parse of a raw (title, artist) pair into a clean Song.

    `source` is an optional hint ("spotify", "youtube_music", "firefox", ...)
    used only to disambiguate "Song - Artist" vs "Artist - Song" ordering;
    the parser degrades gracefully without it.
    """
    title = _strip_translation_suffix(_strip_noise(_strip_browser_chrome(raw_title or "")))
    artist, artist_field_confidence = _resolve_artist_field(raw_artist or "")

    # Player already gives a (now-cleaned) artist field and the title has no
    # further "A - B" ambiguity to resolve.
    if artist and " - " not in title:
        return Song(artist=artist, title=title.strip(" -–|"), artist_confidence=artist_field_confidence)

    parts = [p.strip() for p in re.split(r"\s[-–]\s", title) if p.strip()]
    known_uploader_stripped = len(parts) >= 3 and _looks_like_uploader(parts[0])
    if known_uploader_stripped:
        parts = parts[1:]

    if len(parts) >= 2:
        first, second = parts[0], parts[1]
        if source == "youtube_music":
            return Song(artist=second, title=first, artist_confidence="high")
        if artist and _artist_agrees_with_title_guess(artist, first):
            # The metadata artist field and the title's own split agree —
            # strong signal, keep whatever confidence the field itself earned.
            return Song(artist=artist, title=second, artist_confidence=artist_field_confidence)
        if artist:
            # Metadata disagrees with the title's own "Artist - Song" split.
            # The metadata field is very likely just the uploader/channel
            # name (e.g. "Pizza Music" for a "Lady Gaga - Bad Romance"
            # upload) — prefer the title's guess instead, but it's still a
            # guess, so mark it low-confidence.
            return Song(artist=first, title=second, artist_confidence="low")
        # No metadata artist at all — `first` is our only guess, and it's
        # just as likely to be an uploader/channel name we don't recognize.
        confidence = "high" if known_uploader_stripped else "low"
        return Song(artist=first, title=second, artist_confidence=confidence)

    return Song(artist=artist, title=title, artist_confidence="low" if not artist else artist_field_confidence)
