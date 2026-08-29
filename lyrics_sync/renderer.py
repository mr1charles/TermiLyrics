"""Giant-text renderer: composites glyphs from the font engine into a frame,
with width-aware centering/wrapping, color effects, and plain-text fallback."""
from __future__ import annotations

import colorsys
import random
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .config import RenderConfig
from .fonts import FontEngine
from .languages import romanize as _romanize
from .terminal import ANSI_BRIGHT, ANSI_DIM, ANSI_RESET, rgb_fg

BLOCK_MODE = "block"
PLAIN_MODE = "plain"

DISPLAY_MINIMALIST = "minimalist"
DISPLAY_MUSIC_VIDEO_BOX = "music_video_box"
DISPLAY_HACKER_MATRIX = "hacker_matrix"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

_MATRIX_CHARSET = "ｦｱｳｴｵｶｷｹｺｻｼｽｾｿﾀﾂﾃﾅﾆﾇﾈﾊﾋﾎﾏﾐﾑﾒﾓﾔﾕﾗﾘﾜ0123456789"


@dataclass(frozen=True)
class ColorTheme:
    name: str
    primary: Tuple[int, int, int]
    secondary: Tuple[int, int, int]


# Keys here are what RenderConfig.theme references. Only used when
# gradient=True and rainbow=False (see _color_for).
THEMES: Dict[str, ColorTheme] = {
    "cyberpunk_neon": ColorTheme("Cyberpunk Neon", (255, 0, 170), (0, 255, 255)),
    "classic_mono": ColorTheme("Classic Monochrome", (225, 225, 225), (225, 225, 225)),
    "monokai": ColorTheme("Monokai", (249, 38, 114), (166, 226, 46)),
    "cachyos_green_purple": ColorTheme("CachyOS Green/Purple", (148, 0, 211), (0, 255, 127)),
}
DEFAULT_THEME = THEMES["classic_mono"]


@dataclass
class Frame:
    lines: List[str]


class Renderer:
    def __init__(self, font_engine: FontEngine, config: RenderConfig):
        self.fonts = font_engine
        self.cfg = config
        self._theme = THEMES.get(config.theme, DEFAULT_THEME)
        self._matrix_rng = random.Random()

    # ---- measurement (uses the font engine, so widths are correct
    #      regardless of which glyph source actually drew a character) ----

    def _glyph_width(self, char: str) -> int:
        return len(self.fonts.glyph(char)[0]) + self.cfg.glyph_gap

    def _word_width(self, word: str) -> int:
        return sum(self._glyph_width(c) for c in word)

    def _line_width(self, text: str) -> int:
        words = [w for w in text.split(" ") if w]
        if not words:
            return 0
        space_w = self._glyph_width(" ")
        return sum(self._word_width(w) for w in words) + space_w * (len(words) - 1)

    # ---- glyph-width-aware word wrap ----

    def _segments(self, text: str, cols: int) -> List[Tuple[str, str]]:
        if not text:
            return [(PLAIN_MODE, "")]
        usable = max(cols - 2, 1)
        if cols < self.cfg.min_block_cols:
            return [(PLAIN_MODE, line) for line in self._plain_wrap(text, usable)]

        segments: List[Tuple[str, str]] = []
        cur: List[str] = []
        cur_w = 0
        space_w = self._glyph_width(" ")

        def flush() -> None:
            nonlocal cur, cur_w
            if cur:
                joined = " ".join(cur)
                mode = BLOCK_MODE if self._line_width(joined) <= usable else PLAIN_MODE
                segments.append((mode, joined))
                cur, cur_w = [], 0

        for word in text.split(" "):
            if not word:
                continue
            ww = self._word_width(word)
            gap = space_w if cur else 0
            if cur_w + gap + ww <= usable:
                cur.append(word)
                cur_w += gap + ww
            else:
                flush()
                if ww <= usable:
                    cur, cur_w = [word], ww
                else:
                    for seg in self._hard_wrap(word, usable):
                        segments.append((BLOCK_MODE, seg))
        flush()
        return segments or [(PLAIN_MODE, text)]

    def _hard_wrap(self, word: str, usable: int) -> List[str]:
        result: List[str] = []
        buf: List[str] = []
        buf_w = 0
        for ch in word:
            cw = self._glyph_width(ch)
            if buf_w + cw > usable and buf:
                result.append("".join(buf))
                buf, buf_w = [], 0
            buf.append(ch)
            buf_w += cw
        if buf:
            result.append("".join(buf))
        return result or [word]

    def _plain_wrap(self, text: str, usable: int) -> List[str]:
        words = text.split()
        lines: List[str] = []
        cur: List[str] = []
        cur_w = 0
        for w in words:
            needed = len(w) + (1 if cur else 0)
            if cur_w + needed <= usable:
                cur.append(w)
                cur_w += needed
            else:
                if cur:
                    lines.append(" ".join(cur))
                cur, cur_w = [w], len(w)
        if cur:
            lines.append(" ".join(cur))
        return lines or [text]

    # ---- color effects ----

    def _color_for(self, col_index: int, total_cols: int, row_index: int, total_rows: int) -> str:
        if self.cfg.rainbow:
            hue = (col_index / max(total_cols, 1)) % 1.0
            r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.85, 1.0))
            return rgb_fg(r, g, b)
        if self.cfg.gradient:
            t = row_index / max(total_rows - 1, 1)
            pr, pg, pb = self._theme.primary
            sr, sg, sb = self._theme.secondary
            r = int(pr + (sr - pr) * t)
            g = int(pg + (sg - pg) * t)
            b = int(pb + (sb - pb) * t)
            return rgb_fg(r, g, b)
        return ANSI_BRIGHT

    def _render_block_rows(self, text: str) -> List[str]:
        height = self.fonts.height
        rows = [""] * height
        pad = " " * self.cfg.glyph_gap
        for col_i, ch in enumerate(text):
            glyph = self.fonts.glyph(ch)
            for i in range(height):
                color = self._color_for(col_i, len(text), i, height)
                cell = glyph[i] if i < len(glyph) else " " * len(glyph[0])
                if self.cfg.outline:
                    cell = self._outline(cell)
                rows[i] += f"{color}{cell}{ANSI_RESET}" + pad
        if self.cfg.shadow:
            rows = [f" {ANSI_DIM}{r}{ANSI_RESET}" for r in rows]
        return rows

    @staticmethod
    def _outline(row: str) -> str:
        # Cheap hollow-border effect: thin interior fill so edges read as an outline.
        return row.replace("█", "▓", max(0, len(row) - 2))

    # ---- frame assembly ----

    def _estimated_row_count(self, segments: List[Tuple[str, str]]) -> int:
        height = self.fonts.height
        return sum((height + 1) if mode == BLOCK_MODE else 1 for mode, _ in segments)

    def build_frame(self, lyric_line: str, cols: int, rows: int) -> Frame:
        display = lyric_line or "♪ ♪ ♪"

        if self.cfg.romanize and display != "♪ ♪ ♪":
            display = _romanize(display).text

        segments = self._segments(display, cols)

        # Giant block letters need real vertical room — each wrapped segment
        # costs (glyph height + 1) rows. A narrow terminal (e.g. a
        # quarter-tiled window) can force so many word-wraps that the total
        # comfortably exceeds what's available, and the excess just gets
        # silently truncated by the terminal draw, showing only a
        # fragment of the line. If that's clearly going to happen, fall
        # back to compact plain text instead — still readable, just not
        # giant.
        if self._estimated_row_count(segments) > rows:
            segments = [(PLAIN_MODE, line) for line in self._plain_wrap(display, max(cols - 2, 1))]

        render_lines: List[Tuple[str, str]] = []
        for mode, seg in segments:
            if mode == BLOCK_MODE:
                for prow in self._render_block_rows(seg):
                    render_lines.append(("block", prow))
                render_lines.append(("blank", ""))
            else:
                render_lines.append(("plain", seg))
                render_lines.append(("blank", ""))

        while render_lines and render_lines[-1][0] == "blank":
            render_lines.pop()

        if self.cfg.display_mode == DISPLAY_MUSIC_VIDEO_BOX:
            return self._build_boxed_frame(render_lines, cols, rows)
        if self.cfg.display_mode == DISPLAY_HACKER_MATRIX:
            return self._build_matrix_frame(render_lines, cols, rows)
        return self._build_minimalist_frame(render_lines, cols, rows)

    def _build_minimalist_frame(self, render_lines, cols: int, rows: int) -> Frame:
        top_pad = max((rows - len(render_lines)) // 2, 0)
        out = [""] * top_pad
        out.extend(self._render_lines_centered(render_lines, cols))
        return Frame(lines=out)

    def _build_boxed_frame(self, render_lines, cols: int, rows: int) -> Frame:
        # Wide bordered box: content area is narrower than the full
        # terminal, framed with box-drawing characters — the "Music
        # Video Box" look. Falls back to minimalist if the terminal is
        # too narrow for a border to make sense.
        margin = 4
        box_width = cols - margin * 2
        if box_width < self.cfg.min_block_cols:
            return self._build_minimalist_frame(render_lines, cols, rows)

        border_color = rgb_fg(*self._theme.primary)
        top = border_color + "╭" + "─" * (box_width - 2) + "╮" + ANSI_RESET
        bottom = border_color + "╰" + "─" * (box_width - 2) + "╯" + ANSI_RESET

        content_rows = rows - 2  # minus top/bottom border rows
        top_pad = max((content_rows - len(render_lines)) // 2, 0)
        body: List[str] = [""] * top_pad
        for line in self._render_lines_centered(render_lines, box_width - 4):
            body.append(line)

        side = border_color + "│" + ANSI_RESET
        out = [(" " * margin) + top]
        for line in body[:content_rows]:
            padded = _pad_visible(line, box_width - 4)
            out.append((" " * margin) + side + "  " + padded + "  " + side)
        out.append((" " * margin) + bottom)
        return Frame(lines=out)

    def _build_matrix_frame(self, render_lines, cols: int, rows: int) -> Frame:
        # Same centered content as minimalist, plus a "digital rain" style
        # margin decoration on both sides for atmosphere. Regenerated
        # fresh each frame — since main.py's render loop calls build_frame
        # repeatedly while a line is displayed, the margins animate on
        # their own without any extra timer/state needed here.
        margin_width = 3
        if cols < self.cfg.min_block_cols + margin_width * 2:
            return self._build_minimalist_frame(render_lines, cols, rows)

        content_cols = cols - margin_width * 2
        top_pad = max((rows - len(render_lines)) // 2, 0)
        content = [""] * top_pad
        content.extend(self._render_lines_centered(render_lines, content_cols))

        final = []
        for line in content:
            left = self._matrix_column(margin_width)
            right = self._matrix_column(margin_width)
            padded = _pad_visible(line, content_cols)
            final.append(f"{left}{padded}{right}")
        return Frame(lines=final)

    def _matrix_column(self, width: int) -> str:
        chars = "".join(self._matrix_rng.choice(_MATRIX_CHARSET) for _ in range(width))
        return f"{ANSI_DIM}{rgb_fg(*self._theme.secondary)}{chars}{ANSI_RESET}"

    def _render_lines_centered(self, render_lines, cols: int) -> List[str]:
        out = []
        for kind, content in render_lines:
            if kind == "block":
                out.append(_center_ansi(content, cols))
            elif kind == "plain":
                pad = max((cols - len(content)) // 2, 0)
                out.append(" " * pad + f"{ANSI_BRIGHT}{content}{ANSI_RESET}")
            else:
                out.append("")
        return out


def _visible_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def _center_ansi(s: str, width: int) -> str:
    pad = max((width - _visible_len(s)) // 2, 0)
    return " " * pad + s


def _pad_visible(s: str, width: int) -> str:
    pad = max(width - _visible_len(s), 0)
    return s + " " * pad

