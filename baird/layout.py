"""Full-frame TUI renderer — renders ``UIState`` as a Rich renderable tree.

Every render is a fresh tree built from the current state — nothing is
persistent, everything derives from state each frame.
"""

from __future__ import annotations

import shutil
from typing import Any

from rich.columns import Columns
from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.layout import Layout
from rich.panel import Panel
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text

from .agent_tools import AgentMode
from .theme import (
    BAR,
    BAR_THICK,
    BLOCK_TOOL_MARGIN_TOP,
    BLOCK_TOOL_PADDING_BOTTOM,
    BLOCK_TOOL_PADDING_LEFT,
    BLOCK_TOOL_PADDING_TOP,
    CONTENT_GAP,
    CONTENT_PADDING_BOTTOM,
    CONTENT_PADDING_LEFT,
    CONTENT_PADDING_RIGHT,
    INLINE_TOOL_PADDING_LEFT,
    MSG_MARGIN_TOP,
    MSG_PADDING_BOTTOM,
    MSG_PADDING_LEFT,
    MSG_PADDING_TOP,
    OC,
    SIDEBAR_WIDTH,
    SPINNER_FRAMES,
    TEXT_PART_MARGIN_TOP,
    TEXT_PART_PADDING_LEFT,
)
from .uistate import (
    Dialog,
    Message,
    Part,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    UIState,
)


# ── helpers ────────────────────────────────────────────────────────────


def _agent_badge_style(mode: AgentMode) -> str:
    agent_color = {"build": OC.primary, "plan": OC.secondary, "auto": OC.accent}.get(
        mode.value, OC.textMuted
    )
    return f"bold reverse {agent_color}"


def _tool_icon(name: str) -> str:
    shell_like = {"run_on", "read_remote", "write_remote", "apply_diff_remote"}
    read_like = {"read_file", "list_projects", "list_project_locations", "read_remote"}
    write_like = {"write_file", "apply_diff", "edit_file"}
    search_like = {"glob", "grep", "find"}
    web_like = {"websearch", "research"}
    fetch_like = {"webfetch", "fetch"}
    mgmt = {"register_project", "add_project_location", "todowrite", "set_watch_root"}
    if name in shell_like or name.startswith("run_"):
        return "$"
    if name in read_like:
        return "\u2192"
    if name in write_like:
        return "\u2190"
    if name in search_like:
        return "\u2699"
    if name in web_like:
        return "\u25C7"
    if name in fetch_like:
        return "%"
    if name in mgmt:
        return "\u2699"
    return "\u2699"


# ── header ─────────────────────────────────────────────────────────────


def render_header(state: UIState) -> Text:
    """Compact header:  ┃ {badge}  {project} │ {model}"""
    project = state.project_display or state.project_id or "?"
    return Text.assemble(
        (f"{BAR_THICK} ", OC.textMuted),
        (f" {state.agent_mode.badge} ", _agent_badge_style(state.agent_mode)),
        ("  ", ""),
        (f"{project}", OC.text),
        ("  │  ", OC.textMuted),
        (f"{state.model}", OC.textMuted),
    )


# ── status bar ─────────────────────────────────────────────────────────


def render_status(state: UIState) -> Text:
    """Status line:  │ model=... turns=... cost=..."""
    parts: list[tuple[str, str]] = [
        (f"{BAR}  ", OC.textMuted),
        (f"turns={state.stats.turns}  ", OC.textMuted),
        (f"cost=${state.stats.total_cost_usd:.4f}  ", OC.textMuted),
        (f"tokens={state.stats.total_input_tokens}\u2192{state.stats.total_output_tokens}", OC.text),
    ]
    return Text.assemble(*parts)


# ── sidebar ────────────────────────────────────────────────────────────


def render_sidebar(state: UIState) -> Panel:
    """Session info sidebar — 42-char wide, opencode sidebar style."""
    info_lines = [
        f"  session",
        f"  {state.session_id[:8] if state.session_id else '—'}",
        "",
        f"  project",
        f"  {state.project_id or '—'}",
        "",
        f"  host",
        f"  {state.host_id or '—'}",
        "",
        f"  mode",
        f"  {state.agent_mode.value.upper()}",
        "",
        f"  model",
        f"  {state.model}",
    ]
    if state.branch_display:
        info_lines += ["", "  branch", f"  {state.branch_display}"]
    content = Text("\n".join(info_lines), style=OC.text)
    return Panel(
        content,
        title="info",
        border_style=OC.backgroundElement,
        padding=(0, 0),
        width=SIDEBAR_WIDTH,
    )


# ── user message ───────────────────────────────────────────────────────


def _agent_color(mode: str | None) -> str:
    return {"build": OC.primary, "plan": OC.secondary, "auto": OC.accent}.get(
        mode or "", OC.textMuted
    )


def render_user_message(msg: Message, state: UIState) -> Text:
    """User message with agent-coloured left border (C7 — per-message agent)."""
    border_color = _agent_color(msg.agent_mode or state.agent_mode.value)
    lines = msg.content.strip().splitlines()
    result = Text()
    for i, ln in enumerate(lines):
        if i > 0:
            result.append("\n")
        result.append(f"{BAR}  ", style=border_color)
        result.append(ln, style=OC.text)
    return result


# ── parts ──────────────────────────────────────────────────────────────


def render_text_part(part: TextPart, _state: UIState) -> Text:
    """Assistant text content with 3-char left padding."""
    lines = part.text.strip().splitlines()
    result = Text()
    for i, ln in enumerate(lines):
        if i > 0:
            result.append("\n")
        result.append(f"   {ln}", style=OC.text)
    return result


def render_tool_call_part(part: ToolCallPart, state: UIState) -> Text:
    """Inline tool call:  {icon} {name}({args})"""
    spinner = SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)] if not part.completed else ""
    prefix = f"  {spinner} " if spinner else "     "
    label = f"{part.icon} {part.name}"
    if part.args_preview:
        label += f"({part.args_preview})"
    return Text(f"{prefix}{label}", style=OC.text)


def render_tool_result_part(part: ToolResultPart, state: UIState) -> Text:
    """Tool result as a bordered block — opencode BlockTool style."""
    content_lines = part.result.strip().splitlines()
    is_expanded = state.all_tools_expanded or not part.collapsed
    max_lines = 40 if is_expanded else 8
    if len(content_lines) > max_lines:
        content_lines = content_lines[:max_lines] + [
            f"... ({len(content_lines) - max_lines} more lines, press 'e' to expand)"
        ]
    # Build block
    out = Text()
    out.append(f"{BAR}  {part.icon} {part.name}", style=OC.textMuted)
    out.append("\n")
    for ln in content_lines:
        out.append(f"{BAR}  ", style=OC.textMuted)
        out.append(ln, style=OC.text)
        out.append("\n")
    return out


def render_reasoning_part(part: ReasoningPart, _state: UIState) -> Text:
    """Dimmed reasoning block."""
    icon = "\u25B8" if part.done else SPINNER_FRAMES[0]
    header = f"{icon} {part.title}"
    lines = part.text.strip().splitlines()
    result = Text(f"   {header}", style=OC.textMuted)
    for ln in lines:
        result.append(f"\n   {ln}", style=Style(color=OC.textMuted, dim=True))
    return result


# ── helpers ────────────────────────────────────────────────────────────


def _format_timestamp(ts: float) -> str:
    """Format a unix timestamp as HH:MM:SS."""
    import time
    return time.strftime("%H:%M:%S", time.localtime(ts))


# ── message ────────────────────────────────────────────────────────────


def render_message(msg: Message, state: UIState) -> Text:
    """Render one message with all its parts.

    Includes optional timestamp (C2) and footer agent info (C8).
    """
    result = Text()

    # ── Timestamp line (C2) ──
    if state.show_timestamps and msg.timestamp:
        ts = _format_timestamp(msg.timestamp)
        result.append(f"   {ts}", style=OC.textMuted)
        result.append("\n")

    if msg.role == "user":
        result.append(render_user_message(msg, state))
        return result

    # Assistant message — collect parts
    parts_text = Text()
    for i, part in enumerate(msg.parts):
        if i > 0:
            parts_text.append("\n")
        if isinstance(part, TextPart):
            parts_text.append(render_text_part(part, state))
        elif isinstance(part, ToolCallPart):
            parts_text.append(render_tool_call_part(part, state))
        elif isinstance(part, ToolResultPart):
            if i > 0:
                parts_text.append("\n")
            parts_text.append(render_tool_result_part(part, state))
        elif isinstance(part, ReasoningPart):
            parts_text.append(render_reasoning_part(part, state))

    # If no parts but has content, render directly
    if not msg.parts and msg.content:
        parts_text = render_text_part(TextPart(text=msg.content), state)

    result.append(parts_text)

    # ── Footer agent info (C8) ──
    agent_m = msg.agent_mode or ""
    model_m = msg.model or ""
    dur = msg.duration
    if agent_m or model_m or dur:
        footer_parts = [f"{BAR}  "]
        if agent_m:
            badge = agent_m.upper()
            ac = _agent_color(agent_m)
            footer_parts.append((f" {badge} ", f"reverse {ac}"))
            footer_parts.append(("  ", ""))
        if model_m:
            footer_parts.append((f"{model_m}  ", OC.textMuted))
        if dur:
            footer_parts.append((f"{dur:.1f}s", OC.textMuted))
        result.append("\n")
        result.append(Text.assemble(*footer_parts))

    return result


# ── messages area (scrollable viewport) ───────────────────────────────


def render_messages_area(state: UIState) -> Text:
    """Scrollable message list — viewport clipping (C1) and compaction divider (C6)."""
    # Build list of message renderables
    message_texts: list[Text] = []
    for msg in state.messages:
        if msg.role == "system" and msg.content.startswith("__compacted__"):
            continue  # compaction marker — skip during normal render
        rendered = render_message(msg, state)
        if rendered:
            message_texts.append(rendered)

    # ── Compaction divider (C6) ──
    if state.compacted_count > 0:
        divider = Text()
        label = f" \u2500\u2537 {state.compacted_count} compaction(s) \u253B\u2500 "
        divider.append(f"{BAR}  ", style=OC.textMuted)
        divider.append(label, style=Style(color=OC.textMuted, dim=True))
        message_texts.insert(0, divider)

    # Add pending streaming content (not yet finalized into a message)
    if state.streaming:
        pending = Text()
        if state.pending_text:
            pending.append(render_text_part(TextPart(text=state.pending_text), state))
        for tc in state.pending_tool_calls:
            if not tc.completed:
                pending.append("\n")
                pending.append(render_tool_call_part(tc, state))
        if pending:
            message_texts.append(pending)

    # Join with spacing
    full = Text()
    for i, mt in enumerate(message_texts):
        if i > 0:
            full.append("\n\n")
        full.append(mt)

    # ── Viewport clipping (C1) ──
    lines = full.plain.splitlines()
    total_lines = len(lines)
    # Available height: terminal minus header(1) minus bottom(3: prompt+status+border)
    avail = max(5, state.terminal_height - 5)
    scroll = state.scroll_offset
    # Clamp scroll offset
    max_scroll = max(0, total_lines - avail)
    if scroll > max_scroll:
        scroll = max_scroll
        state.scroll_offset = scroll

    if total_lines <= avail:
        result = full
    else:
        start = total_lines - avail - scroll
        start = max(0, min(start, total_lines - avail))
        visible_lines = lines[start:start + avail]

        # Build scrollbar indicator
        scrollbar_width = 1
        sb = Text()
        for li in range(avail):
            if li > 0:
                sb.append("\n")
            # Thumb position
            thumb_pos = int((start + li) / max(1, total_lines) * avail)
            prev_thumb = int((start + li - 1) / max(1, total_lines) * avail) if li > 0 else -1
            if thumb_pos != prev_thumb:
                sb.append("\u2588", style=OC.textMuted)  # full block
            else:
                sb.append("\u2591", style=Style(color=OC.textMuted, dim=True))  # light shade

        # Render visible lines with scrollbar
        result = Text()
        for li, line in enumerate(visible_lines):
            if li > 0:
                result.append("\n")
            # Truncate long lines to fit
            max_w = max(10, state.terminal_width - scrollbar_width - 4)
            if len(line) > max_w:
                line = line[:max_w - 1] + "\u2026"
            result.append(line, style=OC.text)
            if state.show_scrollbar:
                # Append scrollbar character inline
                sb_line = sb.plain.splitlines()[li] if li < len(sb.plain.splitlines()) else " "
                result.append(" ", style=OC.textMuted)
                result.append(sb_line, style=OC.textMuted)

    return result


# ── dialog overlay ─────────────────────────────────────────────────────


def render_dialog(dlg: Dialog) -> Panel:
    """Modal dialog overlay."""
    body_parts = [dlg.body]
    if dlg.choices:
        body_parts.append("")
        for i, c in enumerate(dlg.choices, 1):
            body_parts.append(f"  {i}. {c}")
        body_parts.append("")
        body_parts.append("  (press number to select, Esc to cancel)")
    content = Text("\n".join(body_parts), style=OC.text)
    return Panel(
        content,
        title=dlg.title,
        border_style=OC.warning,
        padding=(1, 2),
    )


# ── prompt input area ──────────────────────────────────────────────────


def render_prompt_area(state: UIState) -> Text:
    """Input prompt rendered as part of the display."""
    text = state.prompt_text or ""
    # If the cursor is at the end, show it right after the text
    cursor = " " if state.prompt_cursor >= len(text) else ""
    display = text + cursor
    return Text(f"{BAR}  {display}", style=OC.textMuted)


# ── top-level render ───────────────────────────────────────────────────


def render_full_ui(state: UIState) -> Layout:
    """Render the full TUI from state.

    Returns a ``rich.Layout`` that fills the terminal.
    """
    # Update terminal dimensions each frame to handle resize
    terminal_size = shutil.get_terminal_size()
    state.terminal_width = max(40, terminal_size.columns)
    state.terminal_height = max(10, terminal_size.lines)
    width = state.terminal_width
    height = state.terminal_height

    # ── header ──
    header = render_header(state)

    # ── status bar ──
    status = render_status(state)

    # ── messages ──
    messages = render_messages_area(state)

    # ── build layout ──
    if state.sidebar_visible and width > SIDEBAR_WIDTH + 60:
        msg_panel = Panel(messages, border_style=OC.background, padding=(0, 0))
        content = Layout(name="content", ratio=1)
        content.split_row(
            Layout(msg_panel, ratio=1, name="messages"),
            Layout(render_sidebar(state), size=SIDEBAR_WIDTH, name="sidebar"),
        )
    else:
        content = Layout(
            Panel(messages, border_style=OC.background, padding=(0, 0)),
            ratio=1,
            name="content",
        )

    # Bottom area: prompt above status
    bottom = Layout(name="bottom", size=2)
    bottom.split_column(
        Layout(render_prompt_area(state), size=1, name="prompt"),
        Layout(status, size=1, name="status"),
    )

    # Main layout
    layout = Layout()
    layout.split_column(
        Layout(header, size=1, name="header"),
        content,
        bottom,
    )

    return layout


def render_from_state(state: UIState) -> Panel | Layout:
    """Entry point: return a Rich renderable for the full screen.

    If a dialog is active, the dialog fills the screen.
    Otherwise the full layout is returned.
    """
    if state.dialog is not None:
        return render_dialog(state.dialog)
    return render_full_ui(state)
