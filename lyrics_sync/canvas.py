"""A cell grid every display mode paints into, serialized to ANSI once per
frame.

Painting into cells (instead of concatenating escape-coded strings) is what
makes layered animation practical: a bouncing ball can be drawn over text,
digital rain can run behind it, and every frame is guaranteed to be exactly
the terminal's width — no stray wrap can shove the layout down a row.

Double-width characters (CJK, fullwidth forms) occupy two cells; combining
marks attach to the cell before them.
"""
from __future__ import annotations

import unicodedata
from typing import Dict, List, Optional, Tuple

from .terminal import RGB, fg_params

FLAG_BOLD = 1
FLAG_DIM = 2
FLAG_ITALIC = 4
FLAG_REVERSE = 8

Style = Tuple[Optional[RGB], int]   # (foreground color or None, FLAG_* bits)

_WIDTH_CACHE: Dict[str, int] = {}


def char_width(ch: str) -> int:
    if not ch:
        return 0
    w = _WIDTH_CACHE.get(ch)
    if w is not None:
        return w
    c = ch[0]
    if unicodedata.combining(c) or unicodedata.category(c) in ("Mn", "Me", "Cf"):
        w = 0
    elif unicodedata.east_asian_width(c) in ("W", "F"):
        w = 2
    else:
        w = 1
    if len(_WIDTH_CACHE) < 8192:
        _WIDTH_CACHE[ch] = w
    return w


def text_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    if text_width(text) <= width:
        return text
    out, used = [], 0
    limit = max(width - text_width(ellipsis), 0)
    for c in text:
        w = char_width(c)
        if used + w > limit:
            break
        out.append(c)
        used += w
    return "".join(out) + (ellipsis if width > 0 else "")


def style(color: Optional[RGB] = None, bold: bool = False, dim: bool = False,
          italic: bool = False, reverse: bool = False) -> Style:
    flags = (FLAG_BOLD if bold else 0) | (FLAG_DIM if dim else 0) | \
            (FLAG_ITALIC if italic else 0) | (FLAG_REVERSE if reverse else 0)
    return (tuple(int(c) for c in color) if color is not None else None, flags)  # type: ignore[return-value]


class Canvas:
    def __init__(self, cols: int, rows: int):
        self.cols = max(cols, 0)
        self.rows = max(rows, 0)
        self.chars: List[List[str]] = [[" "] * self.cols for _ in range(self.rows)]
        self.styles: List[List[Optional[Style]]] = [[None] * self.cols for _ in range(self.rows)]
        self.used: List[bool] = [False] * self.rows

    # ---- writing ----

    def _release(self, x: int, y: int) -> None:
        """Free cell (x, y), repairing any wide character it was half of."""
        row = self.chars[y]
        c = row[x]
        if c == "" and x > 0:
            row[x - 1] = " "
        elif c and char_width(c[0]) == 2 and x + 1 < self.cols and row[x + 1] == "":
            row[x + 1] = " "

    def put(self, x: int, y: int, ch: str, st: Optional[Style] = None) -> int:
        """Write one character; returns how many cells it occupies."""
        w = char_width(ch)
        if not (0 <= y < self.rows):
            return w
        if w == 0:
            px = x - 1
            if 0 <= px < self.cols:
                if self.chars[y][px] == "" and px > 0:
                    px -= 1
                self.chars[y][px] += ch
            return 0
        if x < 0 or x + w > self.cols:
            return w
        self._release(x, y)
        if w == 2:
            self._release(x + 1, y)
        self.chars[y][x] = ch
        self.styles[y][x] = st
        if w == 2:
            self.chars[y][x + 1] = ""
            self.styles[y][x + 1] = st
        self.used[y] = True
        return w

    def text(self, x: int, y: int, text: str, st: Optional[Style] = None) -> int:
        """Write a string; returns the x just past it."""
        for ch in text:
            x += self.put(x, y, ch, st)
        return x

    def text_centered(self, y: int, text: str, st: Optional[Style] = None,
                      x0: int = 0, width: Optional[int] = None) -> int:
        width = self.cols - x0 if width is None else width
        text = truncate(text, width)
        x = x0 + max((width - text_width(text)) // 2, 0)
        self.text(x, y, text, st)
        return x

    def clear(self, x: int, y: int, w: int, h: int) -> None:
        for yy in range(max(y, 0), min(y + h, self.rows)):
            for xx in range(max(x, 0), min(x + w, self.cols)):
                self._release(xx, yy)
                self.chars[yy][xx] = " "
                self.styles[yy][xx] = None

    def is_blank(self, x: int, y: int) -> bool:
        return 0 <= y < self.rows and 0 <= x < self.cols and self.chars[y][x] == " "

    # ---- layers ----

    def bbox(self) -> Optional[Tuple[int, int, int, int]]:
        """(x, y, w, h) of everything non-blank, or None if empty."""
        xs0, ys0, xs1, ys1 = self.cols, self.rows, -1, -1
        for y in range(self.rows):
            if not self.used[y]:
                continue
            row = self.chars[y]
            for x in range(self.cols):
                if row[x] != " ":
                    xs0, xs1 = min(xs0, x), max(xs1, x)
                    ys0, ys1 = min(ys0, y), max(ys1, y)
        if xs1 < 0:
            return None
        return xs0, ys0, xs1 - xs0 + 1, ys1 - ys0 + 1

    def blit(self, layer: "Canvas") -> None:
        """Draw every non-blank cell of `layer` over this canvas."""
        for y in range(min(self.rows, layer.rows)):
            if not layer.used[y]:
                continue
            lrow, lst = layer.chars[y], layer.styles[y]
            for x in range(min(self.cols, layer.cols)):
                c = lrow[x]
                if c == " " or c == "":
                    continue
                self.put(x, y, c, lst[x])

    # ---- output ----

    def to_lines(self, mode: Optional[str] = None) -> List[str]:
        sgr_cache: Dict[Style, str] = {}

        def sgr(st: Style) -> str:
            code = sgr_cache.get(st)
            if code is None:
                color, flags = st
                params = ["0"]
                if flags & FLAG_BOLD:
                    params.append("1")
                if flags & FLAG_DIM:
                    params.append("2")
                if flags & FLAG_ITALIC:
                    params.append("3")
                if flags & FLAG_REVERSE:
                    params.append("7")
                if color is not None:
                    p = fg_params(color, mode)
                    if p:
                        params.append(p)
                code = "\033[" + ";".join(params) + "m"
                sgr_cache[st] = code
            return code

        lines: List[str] = []
        for y in range(self.rows):
            if not self.used[y]:
                lines.append("")
                continue
            row, sts = self.chars[y], self.styles[y]
            last = self.cols - 1
            while last >= 0 and row[last] == " " and not (sts[last] and sts[last][1] & FLAG_REVERSE):
                last -= 1
            parts: List[str] = []
            cur: Optional[Style] = None
            for x in range(last + 1):
                ch = row[x]
                if ch == "":
                    continue
                st = sts[x]
                if ch == " " and not (st and st[1] & FLAG_REVERSE):
                    st = None
                if st != cur:
                    parts.append(sgr(st) if st is not None else "\033[0m")
                    cur = st
                parts.append(ch)
            if cur is not None:
                parts.append("\033[0m")
            lines.append("".join(parts))
        return lines

    def plain_lines(self) -> List[str]:
        """Text only, no escape codes — for tests and debugging."""
        return ["".join(c for c in row).rstrip() for row in self.chars]
