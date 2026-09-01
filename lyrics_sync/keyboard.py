"""
Non-blocking raw-terminal keypress reading, for the live keybinds
(theme/display-mode/romanize/typing-effect — see renderer.py's
LiveRenderState and main.py's render_loop).

POSIX only (termios/tty). On a platform without them, or when stdin
isn't an actual TTY (piped input, some terminal emulators' odd modes),
this degrades to "no keys ever detected" rather than raising — the app
still runs, live keybinds just silently don't do anything, same as if
the feature didn't exist.
"""

from __future__ import annotations

import sys
from typing import Optional

try:
    import termios
    import tty
    import select
    HAVE_TTY = True
except ImportError:  # e.g. Windows
    HAVE_TTY = False

BACKSPACE_KEYS = ("\x7f", "\x08")
TAB_KEY = "\t"


class RawKeyboard:
    """Context manager: puts the terminal into cbreak mode (unbuffered,
    no waiting for Enter) for the duration of the `with` block, and
    ALWAYS restores the original terminal settings on exit — including
    on an exception or Ctrl-C. Leaving a terminal stuck in raw/cbreak
    mode after the process exits is a real footgun (the shell looks
    broken — no visible input, weird line handling — until the user
    runs `stty sane` or opens a new terminal), so restoration happens
    in every exit path, not just the clean one."""

    def __init__(self) -> None:
        self._fd: Optional[int] = None
        self._old_settings = None

    def __enter__(self) -> "RawKeyboard":
        if HAVE_TTY and sys.stdin.isatty():
            self._fd = sys.stdin.fileno()
            self._old_settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._fd is not None and self._old_settings is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
        return False  # never swallow exceptions

    def poll(self) -> Optional[str]:
        """Returns a single character if one is waiting on stdin right
        now, else None immediately — never blocks the caller. Meant to
        be called once per render-loop tick."""
        if self._fd is None:
            return None
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if not ready:
            return None
        try:
            return sys.stdin.read(1)
        except (OSError, ValueError):
            return None
