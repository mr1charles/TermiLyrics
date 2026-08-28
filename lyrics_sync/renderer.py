"""Giant-text renderer: composites glyphs from the font engine into a frame,
with width-aware centering/wrapping, color effects, and plain-text fallback."""
from __future__ import annotations

import colorsys
import re
from dataclasses import dataclass
from typing import List, Tuple

from .config import RenderConfig
from .fonts import FontEngine
from .terminal import ANSI_BRIGHT, ANSI_DIM, ANSI_RESET, rgb_fg

BLOCK_MODE = "block"
PLAIN_MODE = "plain"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


@dataclass
class Frame:
    lines: List[str]


class Renderer:
    def __init__(self, font_engine: FontEngine, config: RenderConfig):
        self.fonts = font_engine
        self.cfg = config

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
            r = int(120 + 135 * t)
            g = int(160 + 60 * (1 - t))
            return rgb_fg(r, g, 255)
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

        top_pad = max((rows - len(render_lines)) // 2, 0)
        out = [""] * top_pad
        for kind, content in render_lines:
            if kind == "block":
                out.append(_center_ansi(content, cols))
            elif kind == "plain":
                pad = max((cols - len(content)) // 2, 0)
                out.append(" " * pad + f"{ANSI_BRIGHT}{content}{ANSI_RESET}")
            else:
                out.append("")
        return Frame(lines=out)


def _visible_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def _center_ansi(s: str, width: int) -> str:
    pad = max((width - _visible_len(s)) // 2, 0)
    return " " * pad + s
