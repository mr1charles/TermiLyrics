"""Frame renderer: turns "where are we in the song" into a screenful.

Display modes (Tab / number keys):

  1 minimalist       one line in giant letters — the classic look
  2 karaoke          sing-along: a ball bounces from word to word as each
                     one is sung, and words light up as you go
  3 scroll           the whole lyric sheet, gliding upward, current line lit
  4 word_pop         word by word: each word pops up giant as it's sung
  5 music_video_box  giant text in a frame, song title and progress on it
  6 hacker_matrix    giant text over falling digital rain

Line-change animations (e): none, fade, slide, typewriter, scramble, drop.

Everything is painted into a canvas.Canvas and serialized once; Frame.
animating tells the app loop whether anything on screen is moving (so a
static frame isn't redrawn 30 times a second).
"""
from __future__ import annotations

import bisect
import math
import random
import weakref
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .canvas import Canvas, Style, char_width, style, text_width, truncate
from .config import RenderConfig
from .effects import ArtistFx, beat_level, paint_effect
from .fonts import SIZE_BIG, SIZE_MEDIUM, FontEngine
from .languages import detect_script, romanize as _romanize
from .lyrics import Word
from .terminal import ANSI_BRIGHT, ANSI_DIM, ANSI_RESET, color_mode, rgb_fg
from .themes import BACKDROP, DEFAULT_THEME, THEME_KEYS, THEMES, ColorTheme, hue, lerp
from .timing import EMPTY_CURSOR, Cursor, LyricTimeline, estimate_words

BLOCK_MODE = "block"
PLAIN_MODE = "plain"

DISPLAY_MINIMALIST = "minimalist"
DISPLAY_KARAOKE = "karaoke"
DISPLAY_SCROLL = "scroll"
DISPLAY_WORD_POP = "word_pop"
DISPLAY_MUSIC_VIDEO_BOX = "music_video_box"
DISPLAY_HACKER_MATRIX = "hacker_matrix"
DISPLAY_MODES: Tuple[str, ...] = (DISPLAY_MINIMALIST, DISPLAY_KARAOKE, DISPLAY_SCROLL, DISPLAY_WORD_POP,
                                  DISPLAY_MUSIC_VIDEO_BOX, DISPLAY_HACKER_MATRIX)
DISPLAY_LABELS: Dict[str, str] = {
    DISPLAY_MINIMALIST: "Minimalist (giant text)",
    DISPLAY_KARAOKE: "Sing-Along (bouncing ball)",
    DISPLAY_SCROLL: "Lyrics Sheet (scrolling)",
    DISPLAY_WORD_POP: "Word by Word",
    DISPLAY_MUSIC_VIDEO_BOX: "Music Video Box",
    DISPLAY_HACKER_MATRIX: "Hacker Matrix",
}
MODE_ALIASES: Dict[str, str] = {
    "1": DISPLAY_MINIMALIST, "minimal": DISPLAY_MINIMALIST, "normal": DISPLAY_MINIMALIST,
    "2": DISPLAY_KARAOKE, "singalong": DISPLAY_KARAOKE, "sing-along": DISPLAY_KARAOKE,
    "sing_along": DISPLAY_KARAOKE, "ball": DISPLAY_KARAOKE,
    "3": DISPLAY_SCROLL, "sheet": DISPLAY_SCROLL, "list": DISPLAY_SCROLL,
    "4": DISPLAY_WORD_POP, "word": DISPLAY_WORD_POP, "words": DISPLAY_WORD_POP, "pop": DISPLAY_WORD_POP,
    "5": DISPLAY_MUSIC_VIDEO_BOX, "box": DISPLAY_MUSIC_VIDEO_BOX, "video": DISPLAY_MUSIC_VIDEO_BOX,
    "6": DISPLAY_HACKER_MATRIX, "matrix": DISPLAY_HACKER_MATRIX, "hacker": DISPLAY_HACKER_MATRIX,
}

ANIM_NONE = "none"
ANIM_FADE = "fade"
ANIM_SLIDE = "slide"
ANIM_TYPEWRITER = "typewriter"
ANIM_SCRAMBLE = "scramble"
ANIM_DROP = "drop"
ANIMATIONS: Tuple[str, ...] = (ANIM_FADE, ANIM_SLIDE, ANIM_TYPEWRITER, ANIM_SCRAMBLE, ANIM_DROP, ANIM_NONE)

TEXT_STYLE_AUTO = "auto"
TEXT_STYLE_BLOCK = "block"
TEXT_STYLE_MEDIUM = "medium"
TEXT_STYLE_PLAIN = "plain"
TEXT_STYLES: Tuple[str, ...] = (TEXT_STYLE_AUTO, TEXT_STYLE_BLOCK, TEXT_STYLE_MEDIUM, TEXT_STYLE_PLAIN)

_MATRIX_CHARSET = "ｦｱｳｴｵｶｷｹｺｻｼｽｾｿﾀﾂﾃﾅﾆﾇﾈﾊﾋﾎﾏﾐﾑﾒﾓﾔﾕﾗﾘﾜ0123456789"
_SCRAMBLE_CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#%&*+=?"
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_NOTES = "♪♫♪"

# Re-exported for compatibility with code that imported these from here.
__all__ = ["ColorTheme", "THEMES", "THEME_KEYS", "DEFAULT_THEME", "Renderer", "LiveRenderState",
           "Frame", "Scene", "StatusInfo", "DISPLAY_MODES", "ANIMATIONS", "TEXT_STYLES"]


def _ease_out(p: float) -> float:
    p = 0.0 if p < 0 else 1.0 if p > 1 else p
    return 1 - (1 - p) ** 3


def _clamp01(p: float) -> float:
    return 0.0 if p < 0 else 1.0 if p > 1 else p


def fmt_time(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0 or math.isinf(seconds) or math.isnan(seconds):
        return "-:--"
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _cycle(options: Sequence[str], current: str, step: int = 1) -> str:
    i = options.index(current) if current in options else -1
    return options[(i + step) % len(options)]


# --------------------------------------------------------------------------
# Live, keybind-adjustable state
# --------------------------------------------------------------------------

@dataclass
class LiveRenderState:
    """Mutable, in-app-adjustable settings — deliberately NOT frozen,
    unlike every other config dataclass in this project. Changed live via
    keybinds (see main.py), persisted between runs (see settings.py).
    `version` is bumped on every change so the app loop knows to redraw."""
    theme: str
    display_mode: str
    romanize: bool
    typing_effect: bool
    text_style: str = TEXT_STYLE_AUTO
    animation: str = ANIM_FADE
    status_bar: bool = True
    effects: bool = True          # artist effects (stage lights, confetti)
    version: int = 0

    @classmethod
    def from_config(cls, cfg: RenderConfig) -> "LiveRenderState":
        animation = ANIM_TYPEWRITER if cfg.typing_effect else cfg.animation
        if animation not in ANIMATIONS:
            animation = ANIM_FADE
        text_style = cfg.text_style if cfg.text_style in TEXT_STYLES else TEXT_STYLE_AUTO
        return cls(theme=cfg.theme if cfg.theme in THEMES else "classic_mono",
                   display_mode=cfg.display_mode if cfg.display_mode in DISPLAY_MODES else DISPLAY_MINIMALIST,
                   romanize=cfg.romanize, typing_effect=animation == ANIM_TYPEWRITER,
                   text_style=text_style, animation=animation, status_bar=cfg.status_bar)

    def _changed(self) -> None:
        self.version += 1
        self.typing_effect = self.animation == ANIM_TYPEWRITER

    def cycle_theme(self, step: int = 1) -> None:
        self.theme = _cycle(THEME_KEYS, self.theme, step)
        self._changed()

    def cycle_display_mode(self, step: int = 1) -> None:
        self.display_mode = _cycle(DISPLAY_MODES, self.display_mode, step)
        self._changed()

    def set_display_mode(self, mode: str) -> None:
        if mode in DISPLAY_MODES:
            self.display_mode = mode
            self._changed()

    def cycle_text_style(self) -> None:
        self.text_style = _cycle(TEXT_STYLES, self.text_style)
        self._changed()

    def cycle_animation(self) -> None:
        self.animation = _cycle(ANIMATIONS, self.animation)
        self._changed()

    def toggle_romanize(self) -> None:
        self.romanize = not self.romanize
        self._changed()

    def toggle_typing_effect(self) -> None:
        self.animation = ANIM_FADE if self.animation == ANIM_TYPEWRITER else ANIM_TYPEWRITER
        self._changed()

    def toggle_status_bar(self) -> None:
        self.status_bar = not self.status_bar
        self._changed()

    def toggle_effects(self) -> None:
        self.effects = not self.effects
        self._changed()

    def to_dict(self) -> Dict[str, object]:
        return {"theme": self.theme, "display_mode": self.display_mode, "romanize": self.romanize,
                "text_style": self.text_style, "animation": self.animation, "status_bar": self.status_bar,
                "effects": self.effects}

    def apply_dict(self, data: Dict[str, object]) -> None:
        if data.get("theme") in THEMES:
            self.theme = str(data["theme"])
        if data.get("display_mode") in DISPLAY_MODES:
            self.display_mode = str(data["display_mode"])
        if data.get("text_style") in TEXT_STYLES:
            self.text_style = str(data["text_style"])
        if data.get("animation") in ANIMATIONS:
            self.animation = str(data["animation"])
        if isinstance(data.get("romanize"), bool):
            self.romanize = bool(data["romanize"])
        if isinstance(data.get("status_bar"), bool):
            self.status_bar = bool(data["status_bar"])
        if isinstance(data.get("effects"), bool):
            self.effects = bool(data["effects"])
        self._changed()


# --------------------------------------------------------------------------
# Scene description (what main.py hands the renderer each frame)
# --------------------------------------------------------------------------

@dataclass
class StatusInfo:
    title: str = ""
    artist: str = ""
    position: float = 0.0            # player position, seconds
    length: Optional[float] = None
    playing: bool = True
    scale: float = 1.0               # lyric-time scale (slowed < 1 < sped up)
    scale_source: str = "none"
    variant: str = ""
    offset: float = 0.0              # total lyric lead, seconds (+ = earlier)
    word_sync: bool = False          # real word-level timing (vs estimated)
    player_rate: float = 1.0         # the player's own playback speed
    source: str = ""


@dataclass
class Scene:
    now: float = 0.0                              # monotonic wall clock
    cursor: Cursor = EMPTY_CURSOR
    timeline: Optional[LyricTimeline] = None
    scale: float = 1.0                            # lyric seconds per real second
    status: Optional[StatusInfo] = None
    message: str = ""                             # replaces lyrics: "no player", "loading", ...
    message_detail: str = ""
    spinner: bool = False
    toast: str = ""
    show_help: bool = False
    help_lines: List[Tuple[str, str]] = field(default_factory=list)
    fx: Optional[ArtistFx] = None                 # artist effect for the playing track
    beats: Tuple[Tuple[float, float], ...] = ()   # recent (monotonic time, strength) beats


@dataclass
class Frame:
    lines: List[str]
    animating: bool = False


@dataclass
class _Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2


@dataclass
class _Piece:
    word: int        # index of the word it belongs to
    char_start: int  # offset of this piece within the word's text
    text: str
    x: int           # relative to its row's left edge
    w: int


_Layout = List[List[_Piece]]


def layout_words(tokens: Sequence[Tuple[str, bool]], width: int,
                 measure: Callable[[str], int], space_w: int) -> Tuple[_Layout, List[int]]:
    """Greedy word wrap into rows of pieces. A word wider than a whole row
    is split across rows. Returns (rows, row_widths)."""
    rows: _Layout = [[]]
    x = 0
    width = max(width, 1)
    for wi, (text, space_after) in enumerate(tokens):
        tw = sum(measure(c) for c in text)
        if x > 0 and x + tw > width:
            rows.append([])
            x = 0
        if tw > width:
            buf, bw, start = "", 0, 0
            for k, c in enumerate(text):
                cw = measure(c)
                if bw + cw > width and buf:
                    rows[-1].append(_Piece(wi, start, buf, x, bw))
                    rows.append([])
                    x, buf, bw, start = 0, "", 0, k
                buf += c
                bw += cw
            if buf:
                rows[-1].append(_Piece(wi, start, buf, x, bw))
                x += bw
        else:
            rows[-1].append(_Piece(wi, 0, text, x, tw))
            x += tw
        if space_after:
            x += space_w
    rows = [r for r in rows if r] or [[]]
    widths = [max((p.x + p.w for p in r), default=0) for r in rows]
    return rows, widths


# --------------------------------------------------------------------------
# Renderer
# --------------------------------------------------------------------------

class Renderer:
    def __init__(self, font_engine: FontEngine, config: RenderConfig,
                 live: Optional[LiveRenderState] = None):
        self.fonts = font_engine
        self.cfg = config
        self.live = live if live is not None else LiveRenderState.from_config(config)
        self._matrix_rng = random.Random()
        self._prep_cache: Dict[Tuple[str, bool], Tuple[str, bool]] = {}
        self._width_cache: Dict[Tuple[str, str], int] = {}
        # Keyed by the timeline object itself (weakly) — never by id(), which
        # Python reuses once an old song's timeline is garbage-collected.
        self._words_cache: "weakref.WeakKeyDictionary[LyricTimeline, Dict[Tuple[int, bool], Tuple[Word, ...]]]" = \
            weakref.WeakKeyDictionary()
        self._sheet_cache: "weakref.WeakKeyDictionary[LyricTimeline, Dict[Tuple[int, bool], tuple]]" = \
            weakref.WeakKeyDictionary()
        self._animating = False
        self._fx: Optional[ArtistFx] = None     # active artist effect (set per frame in render())
        self._pulse = 0.0                       # 0..1 beat flash level for the text
        self._scene = Scene()

    @property
    def _theme(self) -> ColorTheme:
        fx = getattr(self, "_fx", None)
        if fx is not None:
            return fx.theme
        return THEMES.get(self.live.theme, DEFAULT_THEME)

    # ---- text preparation ----

    def _prepare_text(self, text: str) -> Tuple[str, bool]:
        """Returns (text_to_render, force_plain_mode).

        Non-Latin scripts are the unreliable case for giant block letters:
        the raster Unicode fallback (fonts.py) depends on the system having
        a CJK/Arabic/Hebrew/Devanagari-capable font installed. So:
          1. romanize it (Latin glyphs are guaranteed), or
          2. if no romanizer is installed for that script, render it as
             normal-sized text, which uses the *terminal's* own font
             (near-universal Unicode coverage) — readable, just not giant.
        """
        key = (text, self.live.romanize)
        hit = self._prep_cache.get(key)
        if hit is not None:
            return hit
        result: Tuple[str, bool]
        script = detect_script(text)
        if script == "latin" or not self.live.romanize:
            # Romanize off = the user asked for the native script. Plain
            # text is the reliable way to show it (in auto size); an
            # explicit block/medium size still tries the raster font.
            result = (text, script != "latin")
        else:
            r = _romanize(text)
            result = (r.text, False) if r.was_romanized else (text, True)
        if len(self._prep_cache) > 2048:
            self._prep_cache.clear()
        self._prep_cache[key] = result
        return result

    def _line_words(self, tl: LyricTimeline, i: int) -> Tuple[Tuple[Word, ...], bool]:
        """Display words for line i (timed), and whether block letters are
        off-limits for it. Romanized lines are re-timed over the same sung
        span as the original words."""
        text, force_plain = self._prepare_text(tl.lines[i].text)
        per_tl = self._words_cache.setdefault(tl, {})
        key = (i, self.live.romanize)
        words = per_tl.get(key)
        if words is None:
            words = tl.words[i]
            if text != tl.lines[i].text and words:
                words = estimate_words(text, words[0].start, None, end=words[-1].end)
            per_tl[key] = words
        return words, force_plain

    # ---- measurement ----

    def _gap(self, size: str) -> int:
        return self.cfg.glyph_gap if size == SIZE_BIG else 1

    def _measure(self, size: str) -> Callable[[str], int]:
        if size == PLAIN_MODE:
            return char_width

        def measure(c: str) -> int:
            key = (c, size)
            w = self._width_cache.get(key)
            if w is None:
                w = len(self.fonts.glyph(c, size)[0]) + self._gap(size)
                self._width_cache[key] = w
            return w
        return measure

    def _space_w(self, size: str) -> int:
        if size == PLAIN_MODE:
            return 1
        return (6 if size == SIZE_BIG else 4)

    def _size_height(self, size: str) -> int:
        return 1 if size == PLAIN_MODE else self.fonts.height_for(size)

    def _choose_size(self, tokens: Sequence[Tuple[str, bool]], width: int, height: int,
                     force_plain: bool = False, max_rows: int = 99, band: Optional[Dict[str, int]] = None,
                     row_gap: Optional[int] = None) -> Tuple[str, _Layout, List[int]]:
        """Largest text size whose wrapped layout fits `width` x `height`
        (each row costing band[size] extra rows above it). `force_plain`
        (text giant letters can't reliably draw) only applies in auto mode —
        an explicitly chosen block/medium size still gets attempted."""
        style_pref = self.live.text_style
        if force_plain and style_pref == TEXT_STYLE_AUTO:
            style_pref = TEXT_STYLE_PLAIN
        if style_pref == TEXT_STYLE_PLAIN or width < self.cfg.min_block_cols:
            order = [PLAIN_MODE]
        elif style_pref == TEXT_STYLE_BLOCK:
            order = [SIZE_BIG]
        elif style_pref == TEXT_STYLE_MEDIUM:
            order = [SIZE_MEDIUM]
        else:
            order = [SIZE_BIG, SIZE_MEDIUM, PLAIN_MODE]
        for size in order:
            rows, widths = layout_words(tokens, width, self._measure(size), self._space_w(size))
            if len(order) == 1 or size == PLAIN_MODE:
                return size, rows, widths
            gap = (1 if size != PLAIN_MODE else 0) if row_gap is None else row_gap
            extra = (band or {}).get(size, 0)
            needed = len(rows) * (self._size_height(size) + extra) + (len(rows) - 1) * gap
            if needed <= height and len(rows) <= max_rows:
                return size, rows, widths
        rows, widths = layout_words(tokens, width, char_width, 1)
        return PLAIN_MODE, rows, widths

    # ---- color ----

    def _text_color(self, row_frac: float, col_frac: float) -> Tuple[int, int, int]:
        th = self._theme
        if self.cfg.rainbow or th.animated:
            shift = self._scene.now * 0.08 if th.animated else 0.0
            return hue(col_frac * 0.85 + shift)
        base = lerp(th.primary, th.secondary, row_frac) if self.cfg.gradient else th.primary
        if self._pulse > 0.02:       # artist effect: text brightens on the beat
            return lerp(base, th.highlight, self._pulse * 0.6)
        return base

    def _unsung(self) -> Tuple[int, int, int]:
        th = self._theme
        return lerp(th.primary, th.muted, 0.55)

    # ---- painting primitives ----

    def _paint_glyph(self, canvas: Canvas, x: int, y: int, ch: str, size: str,
                     color_at: Callable[[int, int, int], Optional[Style]], rows_visible: Optional[Tuple[int, int]] = None) -> None:
        """color_at(glyph_row, glyph_col, glyph_width) -> Style or None (skip pixel)."""
        glyph = self.fonts.glyph(ch, size)
        gw = len(glyph[0]) if glyph else 0
        for r, row in enumerate(glyph):
            if rows_visible and not (rows_visible[0] <= r < rows_visible[1]):
                continue
            for c, px in enumerate(row):
                if px == " ":
                    continue
                st = color_at(r, c, gw)
                if st is not None:
                    canvas.put(x + c, y + r, px, st)

    def _paint_layout(self, canvas: Canvas, rect: _Rect, y: int, size: str, rows: _Layout,
                      widths: List[int], tokens: Sequence[Tuple[str, bool]],
                      char_style: Callable[[int, int, int, int, int], Optional[Style]],
                      band: int = 0, row_gap: int = 1,
                      y_offset: Callable[[int], int] = lambda k: 0,
                      char_sub: Optional[Callable[[int, str], str]] = None) -> List[Tuple[int, int, int, int]]:
        """Paint a laid-out line. char_style(global_char_index, word_index,
        glyph_row, glyph_col, glyph_width) -> Style/None. Returns each
        word's (x_center, row_top_y, row_index, piece_width) for the ball."""
        h = self._size_height(size)
        measure = self._measure(size)
        word_pos: Dict[int, Tuple[int, int, int, int]] = {}
        char_base: List[int] = []
        acc = 0
        for text, _ in tokens:
            char_base.append(acc)
            acc += len(text)
        total_chars = max(acc, 1)
        cur_y = y
        for ri, row in enumerate(rows):
            cur_y += band
            left = rect.x + max((rect.w - widths[ri]) // 2, 0)
            for piece in row:
                px = left + piece.x
                for k, ch in enumerate(piece.text):
                    gidx = char_base[piece.word] + piece.char_start + k
                    dy = y_offset(gidx)
                    shown = char_sub(gidx, ch) if char_sub else ch
                    if size == PLAIN_MODE:
                        st = char_style(gidx, piece.word, 0, 0, 1)
                        if st is not None:
                            canvas.put(px, cur_y + dy, shown, st)
                    else:
                        self._paint_glyph(canvas, px, cur_y + dy, shown, size,
                                          lambda r, c, gw, g=gidx, w=piece.word: char_style(g, w, r, c, gw))
                    px += measure(ch)  # advance by the real character, so substitutes never shift the line
                if piece.word not in word_pos or piece.char_start == 0:
                    word_pos[piece.word] = (left + piece.x + piece.w // 2, cur_y, ri, piece.w)
            cur_y += h + row_gap
        return [word_pos.get(i, (rect.cx, y + band, 0, 1)) for i in range(len(tokens))]

    def _layout_height(self, size: str, n_rows: int, band: int = 0, row_gap: int = 1) -> int:
        return n_rows * (self._size_height(size) + band) + max(n_rows - 1, 0) * row_gap

    # ---- transitions ----

    def _transition(self, since: float, n_chars: int, line_duration: float):
        """(visible(gidx) -> bool, color_mix(gidx) -> 0..1 alpha,
        y_offset(gidx) -> rows, scrambled(gidx) -> bool, active)."""
        anim = self.live.animation
        if since < 0:
            since = 0.0
        if anim == ANIM_FADE:
            a = _ease_out(since / 0.35)
            return (lambda g: True), (lambda g: a), (lambda g: 0), (lambda g: False), a < 1
        if anim == ANIM_SLIDE:
            p = _ease_out(since / 0.3)
            off = int(round((1 - p) * 3))
            return (lambda g: True), (lambda g: p), (lambda g: off), (lambda g: False), p < 1
        if anim == ANIM_TYPEWRITER:
            cps = max(self.cfg.typing_chars_per_second, n_chars / max(line_duration * 0.6, 0.2))
            shown = since * cps
            return (lambda g: g < shown), (lambda g: 1.0), (lambda g: 0), (lambda g: False), shown < n_chars
        if anim == ANIM_SCRAMBLE:
            step = min(0.03, 0.45 / max(n_chars, 1))
            done = since >= 0.1 + n_chars * step
            return ((lambda g: True), (lambda g: 1.0 if since >= 0.1 + g * step else 0.55), (lambda g: 0),
                    (lambda g: since < 0.1 + g * step), not done)
        if anim == ANIM_DROP:
            step = min(0.025, 0.4 / max(n_chars, 1))

            def p_of(g: int) -> float:
                return _ease_out((since - g * step) / 0.22)
            done = since >= n_chars * step + 0.22
            return ((lambda g: since >= g * step), (lambda g: p_of(g)),
                    (lambda g: -int(round((1 - p_of(g)) * 3))), (lambda g: False), not done)
        return (lambda g: True), (lambda g: 1.0), (lambda g: 0), (lambda g: False), False

    def _scramble_char(self, gidx: int) -> str:
        rng = random.Random(gidx * 7919 + int(self._scene.now * 18))
        return rng.choice(_SCRAMBLE_CHARSET)

    # ======================================================================
    # Public API
    # ======================================================================

    def render(self, scene: Scene, cols: int, rows: int) -> Frame:
        self._scene = scene
        self._animating = False
        self._fx = scene.fx if (self.live.effects and scene.fx is not None
                                and self.live.display_mode != DISPLAY_HACKER_MATRIX) else None
        self._pulse = beat_level(scene.beats, scene.now) if self._fx is not None else 0.0
        canvas = Canvas(cols, rows)
        if cols <= 0 or rows <= 0:
            return Frame([], False)

        bottom = rows
        show_status = self.live.status_bar and scene.status is not None and rows >= 8 \
            and self.live.display_mode != DISPLAY_MUSIC_VIDEO_BOX
        if show_status:
            bottom -= 1
        area = _Rect(0, 0, cols, bottom)
        mode = self.live.display_mode

        if mode == DISPLAY_MUSIC_VIDEO_BOX:
            area = self._paint_box_frame(canvas, area, scene)
        elif mode == DISPLAY_HACKER_MATRIX:
            self._paint_rain(canvas, area, scene)

        if self._fx is not None:
            # Behind the lyrics: text painters only overwrite the cells they use.
            paint_effect(canvas, area.x, area.y, area.w, area.h, self._fx, scene.now, scene.beats)
            self._animating = True

        target = canvas
        layer: Optional[Canvas] = None
        if mode == DISPLAY_HACKER_MATRIX:
            layer = Canvas(cols, rows)
            target = layer

        if scene.message:
            self._paint_message(target, area, scene)
        elif scene.timeline is not None:
            painter = {
                DISPLAY_KARAOKE: self._paint_karaoke,
                DISPLAY_SCROLL: self._paint_scroll,
                DISPLAY_WORD_POP: self._paint_word_pop,
            }.get(mode, self._paint_minimalist)
            painter(target, area, scene)

        if layer is not None:
            box = layer.bbox()
            if box:
                bx, by, bw, bh = box
                canvas.clear(bx - 2, by - 1, bw + 4, bh + 2)
            canvas.blit(layer)

        if show_status and scene.status is not None:
            self._paint_status(canvas, rows - 1, scene.status)
        if scene.toast:
            self._paint_toast(canvas, scene.toast)
        if scene.show_help:
            self._paint_help(canvas, scene)

        return Frame(canvas.to_lines(), self._animating)

    def build_frame(self, lyric_line: str, cols: int, rows: int) -> Frame:
        """Static single-line frame (no timing, no transition). Kept for
        callers of the pre-timeline API."""
        from .lyrics import LyricLine
        tl = LyricTimeline([LyricLine(0.0, lyric_line)] if lyric_line else [])
        cursor = Cursor(1e6, 0, True, 0, 1.0, 1.0, 1e6, -1, math.inf, 0.0, 0.0) if lyric_line else EMPTY_CURSOR
        return self.render(Scene(now=0.0, cursor=cursor, timeline=tl), cols, rows)

    def build_settings_overlay(self, cols: int, rows: int) -> Frame:
        return self.render(Scene(show_help=True, help_lines=self.default_help_lines()), cols, rows)

    def default_help_lines(self, extra: Sequence[Tuple[str, str]] = ()) -> List[Tuple[str, str]]:
        live = self.live
        lines = [
            ("Tab / 1-6", f"display mode   {DISPLAY_LABELS.get(live.display_mode, live.display_mode)}"),
            ("Backspace / c", f"color theme    {self._theme.name}"),
            ("e", f"animation      {live.animation}"),
            ("a", f"text size      {live.text_style}"),
            ("r", f"romanize       {'on' if live.romanize else 'off'}"),
            ("p", f"status bar     {'on' if live.status_bar else 'off'}"),
            ("x", f"artist effects {'on' if live.effects else 'off'}   (stage lights / confetti on the beat)"),
        ]
        lines.extend(extra)
        return lines

    # ======================================================================
    # Mode painters
    # ======================================================================

    # ---- 1 / 5 / 6: minimalist giant text ----

    def _paint_minimalist(self, canvas: Canvas, rect: _Rect, scene: Scene) -> None:
        tl, cur = scene.timeline, scene.cursor
        assert tl is not None
        if not cur.active or cur.index < 0:
            self._paint_idle(canvas, rect, scene, big=True)
            return
        words, force_plain = self._line_words(tl, cur.index)
        text, _ = self._prepare_text(tl.lines[cur.index].text)
        tokens = [(w.text, w.space_after) for w in words] or [(t, True) for t in text.split()]
        width = rect.w - 2
        size, rows, widths = self._choose_size(tokens, width, rect.h, force_plain)
        h = self._layout_height(size, len(rows), row_gap=1 if size != PLAIN_MODE else 0)
        y = rect.y + max((rect.h - h) // 2, 0)

        n_chars = sum(len(t) for t, _ in tokens)
        since = cur.since_start / max(scene.scale, 0.05)
        sung = (tl.sung_end[cur.index] - tl.starts[cur.index]) / max(scene.scale, 0.05)
        visible, alpha, yoff, scrambled, active = self._transition(since, n_chars, sung)
        th = self._theme
        if active or th.animated:
            self._animating = True
        n_rows_px = self._size_height(size)
        total_w = max(max(widths, default=1), 1)

        def char_style(g: int, w: int, r: int, c: int, gw: int) -> Optional[Style]:
            if not visible(g):
                return None
            if scrambled(g):
                return style(th.muted)
            col_frac = (g + (c / max(gw, 1))) / max(n_chars, 1)
            color = self._text_color(r / max(n_rows_px - 1, 1), col_frac)
            a = alpha(g)
            if a < 1:
                color = lerp(BACKDROP, color, a)
            return style(color, bold=size == PLAIN_MODE)

        def char_sub(g: int, ch: str) -> str:
            # Only letters/digits scramble: they share one glyph width, so
            # the line never shifts while it resolves.
            return self._scramble_char(g) if ch.isalnum() and scrambled(g) else ch

        self._paint_layout(canvas, rect, y, size, rows, widths, tokens, char_style,
                           row_gap=1 if size != PLAIN_MODE else 0, y_offset=yoff,
                           char_sub=char_sub if self.live.animation == ANIM_SCRAMBLE and active else None)

    def _paint_idle(self, canvas: Canvas, rect: _Rect, scene: Scene, big: bool) -> None:
        """Between lines: bouncing notes, and countdown dots when the next
        line is a while away."""
        cur = scene.cursor
        th = self._theme
        now = scene.now
        self._animating = True
        use_block = big and self.live.text_style != TEXT_STYLE_PLAIN and rect.w >= 30 and rect.h >= 8
        notes = "♪ ♪ ♪"
        if use_block:
            size = SIZE_BIG if rect.h >= 12 and self.live.text_style != TEXT_STYLE_MEDIUM else SIZE_MEDIUM
            h = self.fonts.height_for(size)
            measure = self._measure(size)
            total = sum(measure(c) for c in notes)
            x = rect.x + max((rect.w - total) // 2, 0)
            base_y = rect.y + max((rect.h - h) // 2, 0)
            k = 0
            for ch in notes:
                if ch != " ":
                    bounce = int(round(max(0.0, math.sin(now * 3.2 - k * 0.9)) * 1.2))
                    lit = self._countdown_lit(cur, k)
                    color = th.highlight if lit else lerp(th.primary, th.muted, 0.35)
                    self._paint_glyph(canvas, x, base_y - bounce, ch, size,
                                      lambda r, c, gw, col=color: style(col))
                    k += 1
                x += measure(ch)
            if cur.next_index >= 0 and cur.gap_length >= 4 and not math.isinf(cur.gap_length):
                self._paint_countdown(canvas, rect.cx, base_y + h + 2, cur)
            return
        y = rect.cy
        x = rect.cx - 2
        for k in range(3):
            bounce = 1 if math.sin(now * 3.2 - k * 0.9) > 0.3 else 0
            color = th.highlight if self._countdown_lit(cur, k) else th.primary
            canvas.put(x + k * 2, y - bounce, _NOTES[k], style(color, bold=True))
        if cur.next_index >= 0 and cur.gap_length >= 4 and not math.isinf(cur.gap_length):
            self._paint_countdown(canvas, rect.cx, y + 2, cur)

    @staticmethod
    def _countdown_lit(cur: Cursor, k: int) -> bool:
        if cur.next_index < 0 or math.isinf(cur.gap_length) or cur.gap_length < 4:
            return False
        return cur.gap_progress >= (k + 1) / 3.0

    def _paint_countdown(self, canvas: Canvas, cx: int, y: int, cur: Cursor) -> None:
        """Three dots that fill up as the next line approaches."""
        th = self._theme
        x = cx - 2
        for k in range(3):
            lo, hi = k / 3.0, (k + 1) / 3.0
            p = _clamp01((cur.gap_progress - lo) / (hi - lo))
            color = lerp(lerp(th.muted, BACKDROP, 0.4), th.highlight, p)
            canvas.put(x + k * 2, y, "●", style(color))

    # ---- 2: karaoke / sing-along ----

    def _paint_karaoke(self, canvas: Canvas, rect: _Rect, scene: Scene) -> None:
        tl, cur = scene.timeline, scene.cursor
        assert tl is not None
        th = self._theme
        t = cur.time
        i = cur.index if cur.active else cur.next_index
        if i < 0:
            self._paint_idle(canvas, rect, scene, big=False)
            return
        self._animating = True

        words, force_plain = self._line_words(tl, i)
        tokens = [(w.text, w.space_after) for w in words]
        prev_i = tl.prev_text_index(i)
        next_i = tl.next_text_index(i)
        context_rows = 0
        if rect.h >= 12:
            context_rows = 4  # prev line + gap above, gap + next line below
        width = rect.w - 4
        bands = {SIZE_BIG: 4, SIZE_MEDIUM: 3, PLAIN_MODE: 3}  # the big ball is two rows tall
        max_rows = 2 if rect.h < 20 else 3 if rect.h < 34 else 4
        size, rows, widths = self._choose_size(tokens, width, rect.h - context_rows, force_plain,
                                               max_rows=max_rows, band=bands, row_gap=0)
        band = bands[size]
        row_gap = 0  # the ball band already separates wrapped rows
        h = self._layout_height(size, len(rows), band=band, row_gap=row_gap)
        y = rect.y + max((rect.h - h) // 2, 0)

        if context_rows:
            if prev_i >= 0 and cur.active:
                ptxt, _ = self._prepare_text(tl.lines[prev_i].text)
                canvas.text_centered(y - 1, ptxt, style(lerp(th.muted, BACKDROP, 0.35)), rect.x, rect.w)
            if next_i >= 0:
                ntxt, _ = self._prepare_text(tl.lines[next_i].text)
                canvas.text_centered(y + h + 1, ntxt, style(th.muted), rect.x, rect.w)

        # Per-word fill: 0 = unsung, 1 = sung, fractional = being sung.
        fills = []
        for w in words:
            if not cur.active or t <= w.start:
                fills.append(0.0)
            elif t >= (w.end or w.start):
                fills.append(1.0)
            else:
                fills.append((t - w.start) / max((w.end or w.start) - w.start, 1e-3))
        char_base, acc = [], 0
        for text, _ in tokens:
            char_base.append(acc)
            acc += len(text)
        unsung = self._unsung()
        lit = th.highlight
        n_px = self._size_height(size)

        def char_style(g: int, wi: int, r: int, c: int, gw: int) -> Optional[Style]:
            k = g - char_base[wi]
            n = max(len(tokens[wi][0]), 1)
            filled = fills[wi] * n - k   # how much of this character is sung
            if size == PLAIN_MODE:
                f = _clamp01(filled)
                return style(lerp(unsung, lit, f), bold=f > 0.5)
            col_filled = _clamp01(filled) >= (c + 0.5) / max(gw, 1)
            if col_filled:
                return style(lit)
            base = self._text_color(r / max(n_px - 1, 1), g / max(acc, 1))
            return style(lerp(base, th.muted, 0.6))

        positions = self._paint_layout(canvas, rect, y, size, rows, widths, tokens, char_style,
                                       band=band, row_gap=row_gap)
        if not words:
            return

        # Countdown dots in the ball band while waiting for the line.
        if not cur.active and cur.gap_length >= 4 and not math.isinf(cur.gap_length) and cur.time_to_next > 1.0:
            first = positions[0]
            self._paint_countdown(canvas, first[0], first[1] - 2, cur)
            return
        self._paint_ball(canvas, rect, words, positions, t, cur, size, band)

    def _ball_at(self, words: Sequence[Word], positions, t: float, cur: Cursor,
                 rect: _Rect) -> Tuple[float, float, int]:
        """(x, height 0..1, row_top_y) of the ball at lyric time t."""
        starts = [w.start for w in words]
        if not cur.active:
            # Pre-roll: hop in from the left edge onto the first word.
            p = _clamp01(1.0 - cur.time_to_next / 1.0)
            x0, y0 = rect.x + 2, positions[0][1]
            x1 = positions[0][0]
            return x0 + (x1 - x0) * p, 4 * p * (1 - p), y0
        wi = bisect.bisect_right(starts, t) - 1
        wi = max(0, min(wi, len(words) - 1))
        a = positions[wi]
        if wi + 1 < len(words) and t < words[wi + 1].start:
            span = max(words[wi + 1].start - words[wi].start, 1e-3)
            p = _clamp01((t - words[wi].start) / span)
            b = positions[wi + 1]
            if a[2] != b[2]:
                # Next word is on the next row: rise on this row, land on that one.
                if p < 0.5:
                    return a[0], 4 * p * (1 - p), a[1]
                return b[0], 4 * p * (1 - p), b[1]
            return a[0] + (b[0] - a[0]) * p, 4 * p * (1 - p), a[1]
        # Last word: one small hop in place while it's sung, then rest.
        w = words[wi]
        p = _clamp01((t - w.start) / max((w.end or w.start) - w.start, 1e-3))
        return a[0], 0.5 * 4 * p * (1 - p), a[1]

    def _paint_ball(self, canvas: Canvas, rect: _Rect, words: Sequence[Word], positions,
                    t: float, cur: Cursor, size: str, band: int) -> None:
        th = self._theme
        band_top_offset = 3  # the ball's arc spans the rows of the band above the text
        for k, lag in ((2, 0.09), (1, 0.045)):
            if not cur.active:
                break
            x, h, row_y = self._ball_at(words, positions, t - lag * max(self._scene.scale, 0.05), cur, rect)
            yy = row_y - 1 - int(round(h * (band_top_offset - 1)))
            if canvas.is_blank(int(round(x)), yy):
                canvas.put(int(round(x)), yy, "·" if k == 2 else "•", style(lerp(th.highlight, BACKDROP, 0.55)))
        x, h, row_y = self._ball_at(words, positions, t, cur, rect)
        xi = int(round(x))
        yy = row_y - 1 - int(round(h * (band_top_offset - 1)))
        if size == SIZE_BIG:
            canvas.put(xi - 1, yy - 1, "▄", style(th.highlight))
            canvas.put(xi, yy - 1, "█", style(th.highlight))
            canvas.put(xi + 1, yy - 1, "▄", style(th.highlight))
            canvas.put(xi - 1, yy, "▀", style(th.highlight))
            canvas.put(xi, yy, "█", style(th.highlight))
            canvas.put(xi + 1, yy, "▀", style(th.highlight))
        else:
            canvas.put(xi, yy, "●", style(th.highlight, bold=True))

    # ---- 3: scrolling lyric sheet ----

    def _sheet(self, tl: LyricTimeline, width: int) -> Tuple[List[int], List[List[str]], List[int]]:
        per_tl = self._sheet_cache.setdefault(tl, {})
        key = (width, self.live.romanize)
        hit = per_tl.get(key)
        if hit is not None:
            return hit
        idx = [i for i, l in enumerate(tl.lines) if l.text]
        wrapped: List[List[str]] = []
        offsets: List[int] = []
        acc = 0
        for i in idx:
            text, _ = self._prepare_text(tl.lines[i].text)
            rows, _ = layout_words([(w, True) for w in text.split()], width, char_width, 1)
            lines = [" ".join(p.text for p in r) for r in rows]
            offsets.append(acc)
            wrapped.append(lines)
            acc += len(lines) + 1
        if len(per_tl) > 8:
            per_tl.clear()
        per_tl[key] = (idx, wrapped, offsets)
        return idx, wrapped, offsets

    def _paint_scroll(self, canvas: Canvas, rect: _Rect, scene: Scene) -> None:
        tl, cur = scene.timeline, scene.cursor
        assert tl is not None
        th = self._theme
        width = max(rect.w - 8, 10)
        idx, wrapped, offsets = self._sheet(tl, width)
        if not idx:
            self._paint_idle(canvas, rect, scene, big=False)
            return
        focus_line = cur.focus_index
        if focus_line < 0:
            focus_line = idx[0]
        fpos = bisect.bisect_left(idx, focus_line)
        fpos = min(fpos, len(idx) - 1)
        since = self._focus_since(tl, cur) / max(scene.scale, 0.05)
        p = _ease_out(since / 0.4)
        prev_off = offsets[fpos - 1] if fpos > 0 else offsets[fpos] - 2
        scroll = prev_off + (offsets[fpos] - prev_off) * p
        if p < 1:
            self._animating = True
        anchor_y = rect.y + max(rect.h * 2 // 5, 1)

        for e, lines in enumerate(wrapped):
            top = anchor_y + int(round(offsets[e] - scroll))
            if top > rect.y + rect.h or top + len(lines) < rect.y:
                continue
            dist = abs(e - fpos)
            is_focus = e == fpos
            line_i = idx[e]
            if is_focus and cur.active and cur.index == line_i:
                self._animating = True
                self._paint_sheet_active(canvas, rect, top, lines, tl, line_i, cur.time)
                continue
            if is_focus:
                color = th.primary
            else:
                fade = min(0.85, 0.18 + (dist - 1) * 0.16)
                color = lerp(th.muted, BACKDROP, fade)
                if e < fpos:
                    color = lerp(color, BACKDROP, 0.15)
            for k, text in enumerate(lines):
                yy = top + k
                if rect.y <= yy < rect.y + rect.h:
                    canvas.text_centered(yy, text, style(color, bold=is_focus), rect.x, rect.w)
        if not cur.active and cur.next_index >= 0 and cur.gap_length >= 4 and not math.isinf(cur.gap_length):
            self._animating = True
            self._paint_countdown(canvas, rect.cx, anchor_y - 2, cur)

    @staticmethod
    def _focus_since(tl: LyricTimeline, cur: Cursor) -> float:
        """Lyric seconds since the list's focus moved to the current line.
        After a gap the focus moved to the upcoming line when the gap began
        — not again when that line starts — otherwise the list would jump
        back and re-scroll to where it already is."""
        if not cur.active:
            return cur.since_start
        i = cur.index
        if i > 0:
            prev = i - 1
            gap_start = tl.clear_at[prev] if tl.lines[prev].text else tl.starts[prev]
            if gap_start < tl.starts[i] - 1e-6:
                return cur.time - gap_start
        return cur.since_start

    def _paint_sheet_active(self, canvas: Canvas, rect: _Rect, top: int, lines: List[str],
                            tl: LyricTimeline, i: int, t: float) -> None:
        th = self._theme
        words, _ = self._line_words(tl, i)
        tokens = [(w.text, w.space_after) for w in words]
        rows, widths = layout_words(tokens, max(rect.w - 8, 10), char_width, 1)
        unsung = lerp(th.primary, th.muted, 0.25)
        fills = [(_clamp01((t - w.start) / max((w.end or w.start) - w.start, 1e-3))) for w in words]
        char_base, acc = [], 0
        for text, _ in tokens:
            char_base.append(acc)
            acc += len(text)

        def char_style(g: int, wi: int, r: int, c: int, gw: int) -> Optional[Style]:
            f = _clamp01(fills[wi] * max(len(tokens[wi][0]), 1) - (g - char_base[wi]))
            return style(lerp(unsung, th.highlight, f), bold=True)
        self._paint_layout(canvas, rect, top, PLAIN_MODE, rows, widths, tokens, char_style, row_gap=0)

    # ---- 4: word by word ----

    def _paint_word_pop(self, canvas: Canvas, rect: _Rect, scene: Scene) -> None:
        tl, cur = scene.timeline, scene.cursor
        assert tl is not None
        th = self._theme
        if not cur.active or cur.index < 0:
            self._paint_idle(canvas, rect, scene, big=True)
            return
        words, force_plain = self._line_words(tl, cur.index)
        if not words:
            return
        t = cur.time
        wi = max(0, min(bisect.bisect_right([w.start for w in words], t) - 1, len(words) - 1))
        word = words[wi]
        since = (t - word.start) / max(scene.scale, 0.05)
        palette = (th.primary, th.secondary, th.highlight)
        color = palette[wi % 3] if not (th.animated or self.cfg.rainbow) else hue(wi * 0.13 + scene.now * 0.05)

        context_h = 3 if rect.h >= 10 else 0
        size, rows, widths = self._choose_size([(word.text, False)], rect.w - 4, rect.h - context_h, force_plain)
        h = self._layout_height(size, len(rows))
        y = rect.y + max((rect.h - h - context_h) // 2, 0)
        px_h = self._size_height(size)
        p = _ease_out(since / 0.14)
        if p < 1:
            self._animating = True
        # "Pop": reveal glyph rows from the middle outward.
        reveal = max(1, int(math.ceil(px_h * p)))
        lo = (px_h - reveal) // 2
        hi = lo + reveal

        def char_style(g: int, w: int, r: int, c: int, gw: int) -> Optional[Style]:
            if size != PLAIN_MODE and not (lo <= r < hi):
                return None
            return style(lerp(th.highlight, color, p) if p < 1 else color, bold=size == PLAIN_MODE)
        self._paint_layout(canvas, rect, y, size, rows, widths, [(word.text, False)], char_style)

        if context_h:
            self._animating = True
            text_rows, text_widths = layout_words([(w.text, w.space_after) for w in words],
                                                  rect.w - 6, char_width, 1)
            tokens = [(w.text, w.space_after) for w in words]
            unsung = th.muted

            def ctx_style(g: int, k: int, r: int, c: int, gw: int) -> Optional[Style]:
                if k < wi:
                    return style(lerp(th.primary, th.muted, 0.2))
                if k == wi:
                    return style(th.highlight, bold=True)
                return style(unsung)
            self._paint_layout(canvas, rect, y + h + 2, PLAIN_MODE, text_rows[:2], text_widths[:2], tokens,
                               ctx_style, row_gap=0)

    # ---- 5: music video box frame ----

    def _paint_box_frame(self, canvas: Canvas, rect: _Rect, scene: Scene) -> _Rect:
        margin = 2 if rect.w < 60 else 4
        box_w = rect.w - margin * 2
        if box_w < self.cfg.min_block_cols + 4 or rect.h < 5:
            return rect
        th = self._theme
        border = style(th.primary)
        x0, y0, y1 = rect.x + margin, rect.y, rect.y + rect.h - 1
        canvas.text(x0, y0, "╭" + "─" * (box_w - 2) + "╮", border)
        canvas.text(x0, y1, "╰" + "─" * (box_w - 2) + "╯", border)
        for yy in range(y0 + 1, y1):
            canvas.put(x0, yy, "│", border)
            canvas.put(x0 + box_w - 1, yy, "│", border)
        st = scene.status
        if st is not None and box_w > 20:
            title = f" ♪ {st.title}" + (f" — {st.artist}" if st.artist else "") + " "
            canvas.text(x0 + 3, y0, truncate(title, box_w - 8), style(th.highlight, bold=True))
            bar_w = max(min(box_w - 22, 40), 6)
            frac = _clamp01(st.position / st.length) if st.length else 0.0
            filled = int(round(frac * bar_w))
            left = f" {fmt_time(st.position)} "
            right = f" {fmt_time(st.length)} "
            x = x0 + max((box_w - (len(left) + bar_w + len(right))) // 2, 1)
            x = canvas.text(x, y1, left, style(th.muted))
            x = canvas.text(x, y1, "━" * filled, style(th.secondary))
            x = canvas.text(x, y1, "─" * (bar_w - filled), style(th.muted))
            canvas.text(x, y1, right, style(th.muted))
        return _Rect(x0 + 2, y0 + 1, box_w - 4, rect.h - 2)

    # ---- 6: digital rain ----

    def _paint_rain(self, canvas: Canvas, rect: _Rect, scene: Scene) -> None:
        self._animating = True
        th = self._theme
        now = scene.now
        head = lerp(th.secondary, (255, 255, 255), 0.6)
        key = (rect.x, rect.w, rect.h)
        if getattr(self, "_rain_key", None) != key:
            cols = []
            for col in range(rect.x, rect.x + rect.w, 2):
                rng = random.Random(col * 2654435761 % 4294967296)
                length = rng.randint(4, 14)
                period = rect.h + length + rng.randint(0, max(rect.h, 1))
                cols.append((col, rng.uniform(5.0, 16.0), length, period, rng.uniform(0, period)))
            self._rain_key, self._rain_cols = key, cols
        tails = [style(lerp(th.secondary, BACKDROP, min(0.9, 0.25 + k / 14 * 0.75))) for k in range(15)]
        head_st = style(head)
        n = len(_MATRIX_CHARSET)
        tick = int(now * 8)
        for col, speed, length, period, phase in self._rain_cols:
            head_y = rect.y + int((phase + now * speed) % period)
            for k in range(length):
                yy = head_y - k
                if not (rect.y <= yy < rect.y + rect.h):
                    continue
                ch = _MATRIX_CHARSET[((col * 131 + yy * 7919 + (tick >> (k & 1))) * 2654435761 >> 7) % n]
                canvas.put(col, yy, ch, head_st if k == 0 else tails[min(int(k * 14 / length), 14)])

    # ---- chrome ----

    def _paint_message(self, canvas: Canvas, rect: _Rect, scene: Scene) -> None:
        th = self._theme
        y = rect.cy - 1
        text = scene.message
        if scene.spinner:
            self._animating = True
            text = f"{_SPINNER[int(scene.now * 12) % len(_SPINNER)]}  {text}"
        canvas.text_centered(y, text, style(th.primary, bold=True), rect.x, rect.w)
        if scene.message_detail:
            for k, line in enumerate(scene.message_detail.split("\n")[:4]):
                canvas.text_centered(y + 2 + k, line, style(th.muted), rect.x, rect.w)

    def _paint_status(self, canvas: Canvas, y: int, st: StatusInfo) -> None:
        th = self._theme
        muted = style(th.muted)
        cols = canvas.cols
        right_time = f"{fmt_time(st.position)} / {fmt_time(st.length)}"
        badges: List[str] = []
        if not st.playing:
            badges.append("⏸ paused")
        if abs(st.scale - 1.0) > 1e-3:
            label = {"slowed": "slowed", "sped_up": "sped up"}.get(st.variant, "speed")
            approx = "≈" if st.scale_source == "guess" else ""
            badges.append(f"{label} {approx}×{st.scale:.2f}")
        if abs(st.player_rate - 1.0) > 1e-3:
            badges.append(f"player {st.player_rate:g}x")
        if abs(st.offset) >= 0.05:
            badges.append(f"offset {st.offset:+.1f}s")
        badges.append("word-sync" if st.word_sync else "line-sync")
        badge_text = "  ·  ".join(badges)

        bar_w = max(min(cols // 5, 30), 0)
        right_len = len(right_time) + (bar_w + 2 if bar_w >= 6 else 0) + 1
        left = f" ♪ {st.title}" + (f" — {st.artist}" if st.artist else "")
        avail_left = cols - right_len - text_width(badge_text) - 4
        if avail_left < 12:
            badge_text = ""
            avail_left = cols - right_len - 2
        x = canvas.text(0, y, truncate(left, max(avail_left, 0)), muted)
        if badge_text:
            canvas.text(x + 2, y, badge_text, style(lerp(th.muted, th.highlight, 0.35)))
        rx = cols - right_len
        if bar_w >= 6:
            frac = _clamp01(st.position / st.length) if st.length else 0.0
            filled = int(frac * bar_w)
            rx = canvas.text(rx, y, "━" * filled, style(th.secondary))
            rx = canvas.text(rx, y, "●" if filled < bar_w else "", style(th.highlight))
            rx = canvas.text(rx, y, "─" * max(bar_w - filled - 1, 0), muted)
            rx += 1
        canvas.text(rx, y, right_time, muted)

    def _paint_toast(self, canvas: Canvas, text: str) -> None:
        th = self._theme
        canvas.text_centered(0, f"  {text}  ", style(th.highlight, bold=True, reverse=True))

    def _paint_help(self, canvas: Canvas, scene: Scene) -> None:
        th = self._theme
        lines = scene.help_lines or self.default_help_lines()
        key_w = max((text_width(k) for k, _ in lines), default=0)
        val_w = max((text_width(v) for _, v in lines), default=0)
        title = " TermiLyrics — keys "
        inner_w = min(max(key_w + 3 + val_w, text_width(title)) + 4, canvas.cols - 2)
        h = min(len(lines) + 4, canvas.rows)
        x0 = max((canvas.cols - inner_w - 2) // 2, 0)
        y0 = max((canvas.rows - h) // 2, 0)
        border = style(th.primary)
        canvas.clear(x0, y0, inner_w + 2, h)
        canvas.text(x0, y0, "╭" + "─" * inner_w + "╮", border)
        canvas.text(x0 + max((inner_w - text_width(title)) // 2, 1), y0, title, style(th.highlight, bold=True))
        for k in range(1, h - 1):
            canvas.put(x0, y0 + k, "│", border)
            canvas.put(x0 + inner_w + 1, y0 + k, "│", border)
        vx = x0 + 3 + key_w + 3
        for k, (key, value) in enumerate(lines[: max(h - 4, 0)]):
            canvas.text(x0 + 3, y0 + 2 + k, truncate(key, inner_w - 2), style(th.highlight, bold=True))
            canvas.text(vx, y0 + 2 + k, truncate(value, max(x0 + inner_w - vx, 0)), style(th.primary))
        canvas.text(x0, y0 + h - 1, "╰" + "─" * inner_w + "╯", border)


# Legacy helpers kept for any external code that used them.
def _visible_len(s: str) -> int:
    import re
    return text_width(re.sub(r"\033\[[0-9;]*m", "", s))


def _center_ansi(s: str, width: int) -> str:
    pad = max((width - _visible_len(s)) // 2, 0)
    return " " * pad + s


def _pad_visible(s: str, width: int) -> str:
    pad = max(width - _visible_len(s), 0)
    return s + " " * pad
