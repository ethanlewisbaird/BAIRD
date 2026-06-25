"""Watch-roots + denylist filtering for the watchdog.

Per the Phase 1 design: each host config declares `watch.roots` (allowlist of
paths to monitor) and `watch.deny` (gitignore-style patterns to skip within
those roots). This module just handles the deny side — `pathspec` does the
heavy lifting with proper gitignore semantics (`**`, leading slash, negation).
"""

from __future__ import annotations

from pathlib import Path

import pathspec


class ScopeFilter:
    """Matches a relative path against gitignore-style deny patterns."""

    def __init__(self, deny_patterns: list[str]) -> None:
        self._spec = pathspec.GitIgnoreSpec.from_lines(deny_patterns)

    def is_denied(self, rel_path: str | Path) -> bool:
        s = str(rel_path).replace("\\", "/")
        return self._spec.match_file(s)
