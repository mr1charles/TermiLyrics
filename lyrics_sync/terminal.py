"""Terminal I/O: size polling, ANSI/color encoding, flicker-free drawing,
cursor and screen lifecycle."""
from __future__ import annotations

import os
import shutil
import signal
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

ANSI_RESET = "\033[0m"
ANSI_BRIGHT = "\033[1m"
ANSI_DIM = "\033[2m"
CLEAR = "\033[H\033[2J"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
ALT_SCREEN_ON = "\033[?1049h"
ALT_SCREEN_OFF = "\033[?1049l"
WRAP_OFF = "\033[?7l"
WRAP_ON = "\033[?7h"
CLEAR_EOL = "\033[K"

COLOR_TRUE = "truecolor"
COLOR_256 = "256"
COLOR_16 = "16"
COLOR_NONE = "none"

RGB = Tuple[int, int, int]


def detect_color_mode(env: Optional[Dict[str, str]] = None) -> str:
    """Best guess at what the terminal can show. TERMILYRICS_COLOR
    (truecolor/256/16/none) overrides; NO_COLOR is honored."""
    env = dict(os.environ) if env is None else env
    forced = env.get("TERMILYRICS_COLOR", "").strip().lower()
    if forced in (COLOR_TRUE, "24bit", COLOR_256, COLOR_16, COLOR_NONE):
        return COLOR_TRUE if forced == "24bit" else forced
    if env.get("NO_COLOR"):
        return COLOR_NONE
    if env.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return COLOR_TRUE
    term = env.get("TERM", "").lower()
    if any(t in term for t in ("kitty", "alacritty", "wezterm", "foot", "ghostty", "direct")):
        return COLOR_TRUE
    if term in ("linux", "vt100", "vt220", "ansi"):
        return COLOR_16
    return COLOR_256


# Set once at startup (see set_color_mode) — the one piece of module state,
# because every color code in the app is encoded through it.
_color_mode = COLOR_TRUE

_ANSI16: List[Tuple[RGB, int]] = [
    ((0, 0, 0), 30), ((205, 0, 0), 31), ((0, 205, 0), 32), ((205, 205, 0), 33),
    ((0, 0, 238), 34), ((205, 0, 205), 35), ((0, 205, 205), 36), ((229, 229, 229), 37),
    ((127, 127, 127), 90), ((255, 0, 0), 91), ((0, 255, 0), 92), ((255, 255, 0), 93),
    ((92, 92, 255), 94), ((255, 0, 255), 95), ((0, 255, 255), 96), ((255, 255, 255), 97),
]
_CUBE = (0, 95, 135, 175, 215, 255)


def set_color_mode(mode: str) -> None:
    global _color_mode
    _color_mode = mode


def color_mode() -> str:
    return _color_mode


def _to_256(r: int, g: int, b: int) -> int:
    def idx(v: int) -> int:
        return min(range(6), key=lambda i: abs(_CUBE[i] - v))
    ri, gi, bi = idx(r), idx(g), idx(b)
    cube = (_CUBE[ri], _CUBE[gi], _CUBE[bi])
    gray_level = round(((r + g + b) / 3 - 8) / 10)
    gray_level = max(0, min(23, gray_level))
    gray = 8 + gray_level * 10
    d_cube = sum((a - c) ** 2 for a, c in zip((r, g, b), cube))
    d_gray = sum((a - gray) ** 2 for a in (r, g, b))
    if d_gray < d_cube:
        return 232 + gray_level
    return 16 + 36 * ri + 6 * gi + bi


def _to_16(r: int, g: int, b: int) -> int:
    return min(_ANSI16, key=lambda e: sum((a - c) ** 2 for a, c in zip((r, g, b), e[0])))[1]


def fg_params(rgb: RGB, mode: Optional[str] = None) -> str:
    """SGR parameters (without ESC[ and m) for a foreground color."""
    mode = mode or _color_mode
    r, g, b = (max(0, min(255, int(c))) for c in rgb)
    if mode == COLOR_TRUE:
        return f"38;2;{r};{g};{b}"
    if mode == COLOR_256:
        return f"38;5;{_to_256(r, g, b)}"
    if mode == COLOR_16:
        return str(_to_16(r, g, b))
    return ""


def rgb_fg(r: int, g: int, b: int) -> str:
    params = fg_params((r, g, b))
    return f"\033[{params}m" if params else ""


@dataclass
class TerminalSize:
    cols: int
    rows: int

    @classmethod
    def current(cls) -> "TerminalSize":
        cols, rows = shutil.get_terminal_size()
        return cls(cols=max(cols, 1), rows=max(rows, 1))


class TerminalSession:
    """Owns the alternate screen, cursor visibility and line wrapping, and
    guarantees all of it is restored on exit/signal — a crash or Ctrl-C
    must never leave the user's shell with a hidden cursor.

    draw() only rewrites rows that changed since the last frame, so an
    animation that moves one bouncing ball costs one row of output, and the
    screen is never cleared mid-frame (which is what made the old renderer
    flicker)."""

    def __init__(self, out=None, alt_screen: bool = True) -> None:
        self._out = out or sys.stdout
        self._alt = alt_screen
        self._restored = False
        self._last: List[str] = []
        self._last_size: Optional[Tuple[int, int]] = None

    def __enter__(self) -> "TerminalSession":
        self._out.write((ALT_SCREEN_ON if self._alt else "") + HIDE_CURSOR + WRAP_OFF + CLEAR)
        self._out.flush()
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        return self

    def _on_signal(self, sig=None, frame=None) -> None:
        self.restore()
        sys.exit(0)

    def restore(self) -> None:
        if self._restored:
            return
        self._restored = True
        self._out.write(ANSI_RESET + WRAP_ON + SHOW_CURSOR + (ALT_SCREEN_OFF if self._alt else "\n"))
        self._out.flush()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.restore()
        return False

    def size(self) -> TerminalSize:
        return TerminalSize.current()

    def invalidate(self) -> None:
        """Force the next draw() to repaint everything."""
        self._last = []
        self._last_size = None

    def draw(self, lines: List[str], size: TerminalSize) -> None:
        dims = (size.cols, size.rows)
        buf: List[str] = []
        if dims != self._last_size:
            buf.append(CLEAR)
            self._last = []
            self._last_size = dims
        previous = self._last
        for row in range(size.rows):
            line = lines[row] if row < len(lines) else ""
            if row < len(previous) and previous[row] == line:
                continue
            buf.append(f"\033[{row + 1};1H{line}{ANSI_RESET}{CLEAR_EOL}")
        self._last = [lines[r] if r < len(lines) else "" for r in range(size.rows)]
        if buf:
            self._out.write("".join(buf))
            self._out.flush()
