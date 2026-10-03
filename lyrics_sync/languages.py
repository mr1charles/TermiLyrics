"""
Script detection and romanization for lyric text.

Important context: RasterUnicodeFont (fonts.py) already rasterizes ANY
Unicode script into giant block-art via Pillow — CJK, Arabic, Hebrew,
Devanagari, etc. all already render correctly without this module.
Romanization here is a *legibility preference* (so someone who can't
read Japanese can see "Konnichiwa" instead of giant kanji art they
can't read), not something required to avoid broken/missing glyphs.

Each backend is a guarded optional import, same pattern as
syncedlyrics in lyrics.py and youtube_transcript_api in transcript.py
— nothing here is a hard dependency, and romanize() always returns
usable text even with zero of these installed (falling back to
NFKD-based accent stripping, which handles Latin-with-diacritics
languages like Vietnamese/Turkish/French using only the stdlib, and
otherwise passes the original text through unchanged).
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

try:
    import pykakasi
    _KAKASI = pykakasi.kakasi()
    HAVE_PYKAKASI = True
except ImportError:
    HAVE_PYKAKASI = False

try:
    from pypinyin import pinyin as _pypinyin_convert, Style as _PinyinStyle
    HAVE_PYPINYIN = True
except ImportError:
    HAVE_PYPINYIN = False

try:
    from korean_romanizer.romanizer import Romanizer as _KoreanRomanizer
    HAVE_KOREAN_ROMANIZER = True
except ImportError:
    HAVE_KOREAN_ROMANIZER = False

try:
    from unidecode import unidecode as _unidecode
    HAVE_UNIDECODE = True
except ImportError:
    HAVE_UNIDECODE = False


# Unicode block ranges used for script detection. Deliberately coarse —
# good enough to route text to the right transliteration backend, not
# meant to be a general-purpose language identifier.
_SCRIPT_RANGES = {
    "hiragana": (0x3040, 0x309F),
    "katakana": (0x30A0, 0x30FF),
    "han": (0x4E00, 0x9FFF),          # Kanji AND Hanzi share this block
    "hangul": (0xAC00, 0xD7A3),
    "cyrillic": (0x0400, 0x04FF),
    "greek": (0x0370, 0x03FF),
    "arabic": (0x0600, 0x06FF),
    "hebrew": (0x0590, 0x05FF),
    "devanagari": (0x0900, 0x097F),
}


def detect_script(text: str) -> str:
    """Returns the dominant script in `text`: 'japanese', 'chinese',
    'korean', 'cyrillic', 'greek', 'arabic', 'hebrew', 'devanagari', or
    'latin' (the default — covers Spanish/German/French/Portuguese/
    Italian/Turkish/Vietnamese, all Latin-script-with-diacritics).

    Japanese vs. Chinese is the one genuinely ambiguous case: Kanji and
    Hanzi share the same Unicode block. Presence of ANY hiragana/
    katakana is a reliable Japanese signal (Chinese text never contains
    them); a Han-only string with no kana defaults to 'chinese' since
    that's the more common case, but a kanji-only Japanese title will
    be misclassified — there's no way to disambiguate from the text
    alone without a language hint the caller doesn't currently have."""
    counts: dict[str, int] = {name: 0 for name in _SCRIPT_RANGES}
    for ch in text:
        cp = ord(ch)
        for name, (lo, hi) in _SCRIPT_RANGES.items():
            if lo <= cp <= hi:
                counts[name] += 1
                break

    if counts["hiragana"] or counts["katakana"]:
        return "japanese"
    if counts["han"]:
        return "chinese"
    if counts["hangul"]:
        return "korean"
    for script in ("cyrillic", "greek", "arabic", "hebrew", "devanagari"):
        if counts[script]:
            return script
    return "latin"


@dataclass(frozen=True)
class RomanizeResult:
    text: str
    was_romanized: bool  # False means this is passthrough/accent-stripped, not true transliteration
    script: str
    backend_available: bool  # False means the right library isn't installed


def romanize(text: str) -> RomanizeResult:
    """Best-effort romanization. Always returns usable text — check
    `.was_romanized` / `.backend_available` if the caller needs to know
    whether real transliteration happened vs. a fallback."""
    if not text.strip():
        return RomanizeResult(text=text, was_romanized=False, script="latin", backend_available=True)

    script = detect_script(text)

    if script == "japanese":
        if not HAVE_PYKAKASI:
            return RomanizeResult(text, False, script, backend_available=False)
        try:
            result = " ".join(item["hepburn"] for item in _KAKASI.convert(text))
            return RomanizeResult(result.strip(), True, script, backend_available=True)
        except Exception as e:  # noqa: BLE001 — third-party lib, don't let it crash rendering
            log.debug("pykakasi failed for %r: %s", text, e)
            return RomanizeResult(text, False, script, backend_available=True)

    if script == "chinese":
        if not HAVE_PYPINYIN:
            return RomanizeResult(text, False, script, backend_available=False)
        try:
            syllables = _pypinyin_convert(text, style=_PinyinStyle.TONE)
            result = " ".join(s[0] for s in syllables if s)
            return RomanizeResult(result.strip(), True, script, backend_available=True)
        except Exception as e:  # noqa: BLE001
            log.debug("pypinyin failed for %r: %s", text, e)
            return RomanizeResult(text, False, script, backend_available=True)

    if script == "korean":
        if not HAVE_KOREAN_ROMANIZER:
            return RomanizeResult(text, False, script, backend_available=False)
        try:
            result = _KoreanRomanizer(text).romanize()
            return RomanizeResult(result.strip(), True, script, backend_available=True)
        except Exception as e:  # noqa: BLE001
            log.debug("korean_romanizer failed for %r: %s", text, e)
            return RomanizeResult(text, False, script, backend_available=True)

    if script in ("arabic", "hebrew"):
        # Unidecode drops the vowels of these abjads ("ylHqh mn byt lbyt"),
        # which is unreadable; the terminal renders the native script fine.
        return RomanizeResult(text, False, script, backend_available=False)

    if script in ("cyrillic", "greek", "devanagari"):
        if not HAVE_UNIDECODE:
            return RomanizeResult(text, False, script, backend_available=False)
        try:
            result = _unidecode(text)
            return RomanizeResult(result.strip(), True, script, backend_available=True)
        except Exception as e:  # noqa: BLE001
            log.debug("unidecode failed for %r: %s", text, e)
            return RomanizeResult(text, False, script, backend_available=True)

    # Latin-script-with-diacritics (Vietnamese, Turkish, French, German,
    # Spanish, Portuguese, Italian, ...): stdlib-only accent stripping,
    # no external dependency needed. Not "true" phonetic romanization —
    # já becomes ja, not a pronunciation guide — but it's a reasonable,
    # dependency-free default, and unidecode (if installed) does a
    # slightly better job for the trickier cases (ø, ß, etc.).
    if HAVE_UNIDECODE:
        try:
            result = _unidecode(text)
            changed = result != text
            return RomanizeResult(result.strip(), changed, script, backend_available=True)
        except Exception as e:  # noqa: BLE001
            log.debug("unidecode failed for %r: %s", text, e)
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )
    return RomanizeResult(stripped.strip(), stripped != text, script, backend_available=True)
