"""Single-key reader for modal prompts (y/n/e/q).

Reads one keystroke without waiting for Enter. Uses termios on Unix; on
non-Unix or non-TTY, falls back to `input()` and takes the first character.
"""

from __future__ import annotations

import sys
from typing import Iterable


def read_key(allowed: Iterable[str] = "ynqe") -> str:
    """Block until the user presses one of `allowed` (case-insensitive),
    Enter, Esc, or Ctrl+C. Returns the lowercased key. Empty string on
    Enter (treated as the default), 'q' on Esc / Ctrl+C."""
    allowed_lower = {c.lower() for c in allowed}
    if not sys.stdin.isatty():
        line = input().strip().lower()
        return (line[0] if line else "") if (not line or line[0] in allowed_lower) else ""

    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if not ch:
                continue
            if ch in ("\r", "\n"):
                return ""
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\x1b":
                return "q"
            low = ch.lower()
            if low in allowed_lower:
                return low
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
