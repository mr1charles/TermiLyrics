"""
Non-blocking raw-terminal keypress reading, for the live keybinds (see
main.py's KEYMAP).

poll() returns one key per call: a printable character ("q", "2", "+"), or
a name for special keys: "left", "right", "up", "down", "shift-tab",
"backspace", "tab", "enter", "esc", "space".

Bytes are read straight from the file descriptor (os.read), never through
sys.stdin's buffered text wrapper — select() can't see data Python has
already pulled into that buffer, which is how the rest of an arrow key's
escape sequence used to get stuck and misread as separate keypresses.

POSIX only (termios/tty). On a platform without them, or when stdin
isn't a TTY, this degrades to "no keys ever detected" rather than raising.
"""

from __future__ import annotations

import os
import sys
from collections import deque
from typing import Deque, Optional

try:
    import termios
    import tty
    import select
    HAVE_TTY = True
except ImportError:  # e.g. Windows
    HAVE_TTY = False

BACKSPACE_KEYS = ("\x7f", "\x08", "backspace")
TAB_KEY = "\t"

KEY_LEFT, KEY_RIGHT, KEY_UP, KEY_DOWN = "left", "right", "up", "down"
KEY_SHIFT_TAB = "shift-tab"
KEY_ESC = "esc"

_SEQUENCES = {
    "\x1b[A": KEY_UP, "\x1b[B": KEY_DOWN, "\x1b[C": KEY_RIGHT, "\x1b[D": KEY_LEFT,
    "\x1bOA": KEY_UP, "\x1bOB": KEY_DOWN, "\x1bOC": KEY_RIGHT, "\x1bOD": KEY_LEFT,
    "\x1b[Z": KEY_SHIFT_TAB,
}
_NAMED = {"\x7f": "backspace", "\x08": "backspace", "\t": "tab", "\r": "enter", "\n": "enter", " ": "space"}


def parse_keys(data: str) -> list:
    """Split raw terminal input into key tokens."""
    keys = []
    i = 0
    while i < len(data):
        c = data[i]
        if c == "\x1b":
            matched = False
            for seq, name in _SEQUENCES.items():
                if data.startswith(seq, i):
                    keys.append(name)
                    i += len(seq)
                    matched = True
                    break
            if matched:
                continue
            if data.startswith("\x1b[", i) or data.startswith("\x1bO", i):
                # Unknown CSI/SS3 sequence (F-keys, mouse, ...): skip it whole.
                j = i + 2
                while j < len(data) and not ("@" <= data[j] <= "~"):
                    j += 1
                i = j + 1
                continue
            keys.append(KEY_ESC)
            i += 1
            continue
        keys.append(_NAMED.get(c, c))
        i += 1
    return keys


class RawKeyboard:
    """Context manager: puts the terminal into cbreak mode (unbuffered,
    no echo, no waiting for Enter) for the duration of the `with` block,
    and ALWAYS restores the original settings on exit — including on an
    exception or Ctrl-C. A terminal left in cbreak mode looks broken
    until the user runs `stty sane`."""

    def __init__(self) -> None:
        self._fd: Optional[int] = None
        self._old_settings = None
        self._pending: Deque[str] = deque()

    def __enter__(self) -> "RawKeyboard":
        if HAVE_TTY and sys.stdin.isatty():
            try:
                self._fd = sys.stdin.fileno()
                self._old_settings = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
            except (termios.error, OSError, ValueError):
                self._fd = None
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._fd is not None and self._old_settings is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
        return False  # never swallow exceptions

    def poll(self) -> Optional[str]:
        """Returns the next key if one is waiting, else None immediately —
        never blocks. Call it until it returns None to drain a burst."""
        if self._pending:
            return self._pending.popleft()
        if self._fd is None:
            return None
        try:
            ready, _, _ = select.select([self._fd], [], [], 0)
            if not ready:
                return None
            data = os.read(self._fd, 256)
            # An escape sequence can straddle two reads; give its tail a moment.
            if data.endswith(b"\x1b") or data.endswith(b"\x1b[") or data.endswith(b"\x1bO"):
                more, _, _ = select.select([self._fd], [], [], 0.02)
                if more:
                    data += os.read(self._fd, 256)
        except (OSError, ValueError):
            return None
        self._pending.extend(parse_keys(data.decode("utf-8", errors="ignore")))
        return self._pending.popleft() if self._pending else None
