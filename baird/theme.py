"""OpenCode theme constants — exact colours, spacing, and unicode glyphs.

All visual constants used by the TUI live here so rendering across
the whole application stays consistent.
"""

from __future__ import annotations

# ── OpenCode dark palette (exact hex from opencode theme JSON) ──────────


class _Colors:
    """All OpenCode theme colour tokens — no surprises."""

    background = "#0a0a0a"
    backgroundPanel = "#141414"
    backgroundElement = "#1e1e1e"
    backgroundMenu = "#1e1e1e"

    text = "#eeeeee"
    textMuted = "#808080"

    primary = "#fab283"
    secondary = "#5c9cf5"
    accent = "#9d7cd8"

    error = "#e06c75"
    warning = "#f5a742"
    success = "#7fd88f"
    info = "#56b6c2"

    border = "#484848"
    borderActive = "#606060"
    borderSubtle = "#3c3c3c"

    diffAdded = "#7fd88f"
    diffRemoved = "#e06c75"
    diffContext = "#808080"
    diffHunkHeader = "#5c9cf5"

    # Diff backgrounds (subtle)
    diffAddedBg = "#1a2e1a"
    diffRemovedBg = "#2e1a1a"

    # Syntax highlighting
    syntaxComment = "#6a9955"
    syntaxString = "#ce9178"
    syntaxNumber = "#b5cea8"
    syntaxKeyword = "#569cd6"
    syntaxFunction = "#dcdcaa"
    syntaxType = "#4ec9b0"

    # Spinner frames (same as OpenCode)
    spinnerFrames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    spinnerInterval = 0.08  # 80ms

    thinkingOpacity = 0.6


# Singleton — use ``OC.primary``, ``OC.text``, etc. everywhere.
OC = _Colors()


# ── Unicode glyphs used in the TUI ──────────────────────────────────────

BAR = "\u2502"  # vertical bar │
BAR_THICK = "\u2503"  # thick vertical bar ┃
CHECK = "\u2713"  # ✓
CROSS = "\u2717"  # ✗
BULLET = "\u25CF"  # ●
AGENT_ICON = "\u25A3"  # ▣


# ── Spacing constants (exact OpenCode values) ─────────────────────────

CONTENT_PADDING_LEFT = 2
CONTENT_PADDING_RIGHT = 2
CONTENT_PADDING_BOTTOM = 1
CONTENT_GAP = 1

MSG_PADDING_TOP = 1
MSG_PADDING_BOTTOM = 1
MSG_PADDING_LEFT = 2
MSG_MARGIN_TOP = 1

TEXT_PART_PADDING_LEFT = 3
TEXT_PART_MARGIN_TOP = 1

INLINE_TOOL_ICON_WIDTH = 2
INLINE_TOOL_PADDING_LEFT = 3

# Convenience aliases
SPINNER_FRAMES: list[str] = OC.spinnerFrames

BLOCK_TOOL_PADDING_TOP = 1
BLOCK_TOOL_PADDING_BOTTOM = 1
BLOCK_TOOL_PADDING_LEFT = 2
BLOCK_TOOL_MARGIN_TOP = 1
BLOCK_TOOL_GAP = 1

SIDEBAR_WIDTH = 42
SIDEBAR_PADDING_LEFT = 2
SIDEBAR_PADDING_RIGHT = 2
SIDEBAR_AUTO_OPEN_MIN_WIDTH = 120
