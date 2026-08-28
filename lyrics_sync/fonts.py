"""Extensible font engine for the giant-letter renderer.

Resolution order for any character:
  1. StaticBlockFont   — hand-authored 5-row glyphs (fastest, best-looking;
                          covers Latin + any diacritics packs dropped into
                          the fonts directory as JSON/YAML).
  2. RasterUnicodeFont — rasterizes the character from a real TrueType font
                          via Pillow and downsamples to block-shade
                          characters. This is what makes CJK, Arabic, Hebrew,
                          Devanagari, Thai, etc. work without hand-authoring
                          thousands of glyphs: any script a system font can
                          draw, we can turn into a giant terminal glyph.
  3. Literal passthrough — last resort so nothing is ever silently dropped.

Community font packs: drop JSON into `paths.fonts_dir`:
    {"glyphs": {"Ä": ["...", "...", "...", "...", "..."]}}
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Protocol

log = logging.getLogger(__name__)

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


BASE_FONT: Dict[str, List[str]] = {
    'A': ["  ███  ", " ██ ██ ", "███████", "██   ██", "██   ██"],
    'B': ["██████ ", "██   ██", "██████ ", "██   ██", "██████ "],
    'C': [" █████ ", "██   ██", "██     ", "██   ██", " █████ "],
    'D': ["██████ ", "██   ██", "██   ██", "██   ██", "██████ "],
    'E': ["███████", "██     ", "█████  ", "██     ", "███████"],
    'F': ["███████", "██     ", "█████  ", "██     ", "██     "],
    'G': [" █████ ", "██     ", "██  ███", "██   ██", " █████ "],
    'H': ["██   ██", "██   ██", "███████", "██   ██", "██   ██"],
    'I': ["███████", "  ██   ", "  ██   ", "  ██   ", "███████"],
    'J': ["     ██", "     ██", "     ██", "██   ██", " █████ "],
    'K': ["██   ██", "██  ██ ", "█████  ", "██  ██ ", "██   ██"],
    'L': ["██     ", "██     ", "██     ", "██     ", "███████"],
    'M': ["██   ██", "███ ███", "███████", "██ █ ██", "██   ██"],
    'N': ["██   ██", "███  ██", "████ ██", "██ ████", "██   ██"],
    'O': [" █████ ", "██   ██", "██   ██", "██   ██", " █████ "],
    'P': ["██████ ", "██   ██", "██████ ", "██     ", "██     "],
    'Q': [" █████ ", "██   ██", "██   ██", "██  ███", " ██████"],
    'R': ["██████ ", "██   ██", "██████ ", "██  ██ ", "██   ██"],
    'S': [" █████ ", "██     ", " █████ ", "     ██", " █████ "],
    'T': ["███████", "  ██   ", "  ██   ", "  ██   ", "  ██   "],
    'U': ["██   ██", "██   ██", "██   ██", "██   ██", " █████ "],
    'V': ["██   ██", "██   ██", "██   ██", " ██ ██ ", "  ███  "],
    'W': ["██   ██", "██   ██", "██ █ ██", "███████", "███ ███"],
    'X': ["██   ██", " ██ ██ ", "  ███  ", " ██ ██ ", "██   ██"],
    'Y': ["██   ██", " ██ ██ ", "  ███  ", "  ██   ", "  ██   "],
    'Z': ["███████", "     ██", "  ███  ", " ██    ", "███████"],
    ' ': ["       ", "       ", "       ", "       ", "       "],
    '0': [" █████ ", "██   ██", "██   ██", "██   ██", " █████ "],
    '1': ["  ██   ", " ███   ", "  ██   ", "  ██   ", "███████"],
    '2': [" █████ ", "██   ██", "   ███ ", " ██    ", "███████"],
    '3': [" █████ ", "██   ██", "  ████ ", "██   ██", " █████ "],
    '4': ["██   ██", "██   ██", "███████", "     ██", "     ██"],
    '5': ["███████", "██     ", "██████ ", "     ██", "██████ "],
    '6': [" █████ ", "██     ", "██████ ", "██   ██", " █████ "],
    '7': ["███████", "     ██", "    ██ ", "   ██  ", "  ██   "],
    '8': [" █████ ", "██   ██", " █████ ", "██   ██", " █████ "],
    '9': [" █████ ", "██   ██", " ██████", "     ██", " █████ "],
    '.': ["  ", "  ", "  ", "  ", "██"],
    '!': ["██", "██", "██", "  ", "██"],
    '?': [" ███ ", "█   █", "   █ ", "     ", "  █  "],
    '-': ["      ", "      ", "██████", "      ", "      "],
    '(': ["  ██ ", " ██  ", " ██  ", " ██  ", "  ██ "],
    ')': [" ██  ", "  ██ ", "  ██ ", "  ██ ", " ██  "],
    "'": [" ██ ", " ██ ", " █  ", "    ", "    "],
    '"': [" █ █ ", " █ █ ", "     ", "     ", "     "],
    ',': ["    ", "    ", "    ", " ██ ", " ██ "],
    # Eighth note — used as the "song playing, no current lyric line" idle
    # placeholder (instrumental intros/breaks/outros). Hand-drawn instead of
    # left to the Pillow raster fallback since it's shown very often.
    '♪': ["  ██  ", "  ██  ", "  ██  ", "███   ", "███   "],
}


class GlyphSource(Protocol):
    def get(self, char: str, height: int) -> Optional[List[str]]: ...


class StaticBlockFont:
    """Hand-authored glyphs, extendable with JSON/YAML packs from fonts_dir."""

    def __init__(self, fonts_dir: Optional[Path] = None, height: int = 5):
        self.height = height
        self.glyphs: Dict[str, List[str]] = dict(BASE_FONT)
        if fonts_dir and fonts_dir.exists():
            self._load_packs(fonts_dir)

    def _load_packs(self, fonts_dir: Path) -> None:
        paths = sorted(fonts_dir.glob("*.json")) + sorted(fonts_dir.glob("*.yaml")) + sorted(fonts_dir.glob("*.yml"))
        for path in paths:
            try:
                if path.suffix == ".json":
                    data = json.loads(path.read_text(encoding="utf-8"))
                elif HAVE_YAML:
                    data = yaml.safe_load(path.read_text(encoding="utf-8"))
                else:
                    continue
                for char, rows in data.get("glyphs", {}).items():
                    if isinstance(rows, list) and len(rows) == self.height:
                        self.glyphs[char] = rows
            except (OSError, json.JSONDecodeError, ValueError, AttributeError) as e:
                log.warning("failed to load font pack %s: %s", path, e)

    def get(self, char: str, height: int) -> Optional[List[str]]:
        if height != self.height:
            return None
        return self.glyphs.get(char) or self.glyphs.get(char.upper())


class RasterUnicodeFont:
    """Renders any Unicode character to block-shade glyphs via Pillow, so
    scripts without hand-authored art still display as giant characters
    instead of being skipped."""

    _SHADES = " .:-=+*#%@█"

    def __init__(self, cache_dir: Optional[Path] = None, font_path: Optional[str] = None):
        self.cache_dir = cache_dir
        self.font_path = font_path
        self._font_cache: Dict[int, "ImageFont.FreeTypeFont"] = {}
        self._mem_cache: Dict[str, List[str]] = {}

    def available(self) -> bool:
        return HAVE_PIL

    def _font_for_size(self, px: int):
        if px in self._font_cache:
            return self._font_cache[px]
        font = None
        candidates = [self.font_path] if self.font_path else []
        candidates += [
            "/usr/share/fonts/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for path in candidates:
            if not path:
                continue
            try:
                font = ImageFont.truetype(path, px)
                break
            except (OSError, IOError):
                continue
        if font is None:
            font = ImageFont.load_default()
        self._font_cache[px] = font
        return font

    def get(self, char: str, height: int) -> Optional[List[str]]:
        if not HAVE_PIL or not char.strip():
            return None

        cache_key = f"{char}_{height}"
        if cache_key in self._mem_cache:
            return self._mem_cache[cache_key]

        disk = self._read_disk_cache(cache_key)
        if disk is not None:
            self._mem_cache[cache_key] = disk
            return disk

        rows = self._rasterize(char, height)
        if rows:
            self._mem_cache[cache_key] = rows
            self._write_disk_cache(cache_key, rows)
        return rows

    def _rasterize(self, char: str, height: int) -> Optional[List[str]]:
        px = height * 16
        font = self._font_for_size(px)
        img = Image.new("L", (px * 2, px * 2), color=0)
        draw = ImageDraw.Draw(img)
        try:
            draw.text((px // 2, 0), char, fill=255, font=font)
        except Exception as e:
            log.debug("rasterize failed for %r: %s", char, e)
            return None

        bbox = img.getbbox()
        if bbox is None:
            return [" " * height for _ in range(height)]
        img = img.crop(bbox)

        target_h = height
        # Block chars are roughly twice as tall as wide, so widen sampling.
        target_w = max(1, round(img.width / img.height * target_h * 2))
        img = img.resize((target_w, target_h))

        rows = []
        for y in range(target_h):
            row = ""
            for x in range(target_w):
                v = img.getpixel((x, y))
                row += self._SHADES[min(v * (len(self._SHADES) - 1) // 255, len(self._SHADES) - 1)]
            rows.append(row)
        return rows

    def _read_disk_cache(self, key: str) -> Optional[List[str]]:
        if not self.cache_dir:
            return None
        path = self.cache_dir / f"{_safe(key)}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_disk_cache(self, key: str, rows: List[str]) -> None:
        if not self.cache_dir:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / f"{_safe(key)}.json").write_text(
                json.dumps(rows, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() else f"u{ord(c):x}" for c in s)[:64]


class FontEngine:
    """Tries every registered source in order; never drops a character."""

    def __init__(self, sources: List[GlyphSource], height: int = 5):
        self.sources = sources
        self.height = height

    def glyph(self, char: str) -> List[str]:
        for source in self.sources:
            rows = source.get(char, self.height)
            if rows:
                return rows
        pad_top = (self.height - 1) // 2
        rows = [" " for _ in range(self.height)]
        rows[pad_top] = char
        return rows
