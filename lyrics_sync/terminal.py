"""Terminal I/O: size polling, ANSI control codes, cursor lifecycle."""
from __future__ import annotations

import shutil
import signal
import sys
from dataclasses import dataclass
from typing import List

ANSI_RESET = "\033[0m"
ANSI_BRIGHT = "\033[1m"
ANSI_DIM = "\033[2m"
CLEAR = "\033[H\033[2J"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


def rgb_fg(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m"


@dataclass
class TerminalSize:
    cols: int
    rows: int

    @classmethod
    def current(cls) -> "TerminalSize":
        cols, rows = shutil.get_terminal_size()
        return cls(cols=cols, rows=rows)


class TerminalSession:
    """Owns cursor visibility and guarantees restoration on exit/signal —
    a crash or Ctrl-C in the old script could leave the cursor hidden."""

    def __init__(self) -> None:
        self._restored = False

    def __enter__(self) -> "TerminalSession":
        sys.stdout.write(HIDE_CURSOR)
        sys.stdout.flush()
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
        sys.stdout.write(SHOW_CURSOR + ANSI_RESET + "\n")
        sys.stdout.flush()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.restore()
        return False

    def size(self) -> TerminalSize:
        return TerminalSize.current()

    def draw(self, lines: List[str], size: TerminalSize) -> None:
        buf = [CLEAR]
        buf.extend(lines[: size.rows])
        buf.extend([""] * max(size.rows - len(lines), 0))
        sys.stdout.write("\n".join(buf))
        sys.stdout.flush()
