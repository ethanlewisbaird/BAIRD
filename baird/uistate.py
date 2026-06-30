"""UI state model — single source of truth for the full-frame TUI.

Every frame of the Live-rendered UI is derived from one ``UIState``
instance.  Mutations to the state trigger a full re-render.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .agent_tools import AgentMode
from .repl import ReplConfig, ReplStats


# ── Part types (mirrors opencode's part-based message model) ──────────


@dataclass
class TextPart:
    """Plain assistant text content."""
    text: str


@dataclass
class ToolCallPart:
    """Inline tool call seen during streaming."""
    name: str
    args_preview: str = ""
    completed: bool = False
    icon: str = "$"


@dataclass
class ToolResultPart:
    """Completed tool result."""
    name: str
    icon: str
    result: str
    collapsed: bool = False  # expand/collapse toggle


@dataclass
class ReasoningPart:
    """Thinking / reasoning block."""
    text: str
    title: str = "Thinking"
    done: bool = False


Part = TextPart | ToolCallPart | ToolResultPart | ReasoningPart


@dataclass
class Message:
    """One conversation message (user or assistant)."""
    role: str  # "user" | "assistant" | "reasoning" | "system"
    content: str
    parts: list[Part] = field(default_factory=list)
    agent_mode: str | None = None    # which agent produced this (C7)
    model: str | None = None         # model used (C8)
    duration: float = 0.0            # seconds for this turn (C8)
    timestamp: float = 0.0           # unix seconds (C2)


# ── Dialog system (modal overlay) ─────────────────────────────────────


@dataclass
class Dialog:
    kind: str  # "confirm" | "prompt" | "info"
    title: str
    body: str
    choices: list[str] = field(default_factory=list)
    result: str | None = None


# ── Main state ────────────────────────────────────────────────────────


@dataclass
class UIState:
    """Single source of truth for the entire TUI.

    All rendering derives from this object every frame.
    """

    # ── Layout
    terminal_width: int = 80
    terminal_height: int = 24
    sidebar_visible: bool = False

    # ── Messages
    messages: list[Message] = field(default_factory=list)
    scroll_offset: int = 0  # lines scrolled from bottom
    show_timestamps: bool = False
    show_scrollbar: bool = True

    # ── Streaming
    streaming: bool = False
    pending_text: str = ""  # accumulated content for in-progress turn
    pending_tool_calls: list[ToolCallPart] = field(default_factory=list)
    tool_results: list[ToolResultPart] = field(default_factory=list)
    turn_active: bool = False  # true while _one_turn is running

    # ── Mode
    agent_mode: AgentMode = AgentMode.BUILD

    # ── Config
    model: str = ""
    project_id: str = ""
    host_id: str | None = None
    session_id: str = ""
    branch: str | None = None
    stats: ReplStats = field(default_factory=ReplStats)

    # ── Dialog overlay
    dialog: Dialog | None = None

    # ── Animation
    spinner_frame: int = 0  # cycled by render timer

    # ── Prompt (managed by input handler)
    prompt_text: str = ""
    prompt_cursor: int = 0

    # ── Expand / collapse (tool output)
    expanded_tools: set[str] = field(default_factory=set)
    all_tools_expanded: bool = False  # global toggle — show full tool output

    # ── Compaction (C6)
    compacted_count: int = 0         # number of times compaction has run
    compacted_messages: list[str] = field(default_factory=list)  # summaries

    # ── Status
    show_conceal: bool = False  # hide tool output content
    last_turn_duration: float = 0.0

    # ── Duck-typed context (avoids import of RepoContext here)
    project_display: str = ""
    host_display: str = ""
    branch_display: str = ""


    # ── Convenience helpers ─────────────────────────────────────────


    @property
    def content_width(self) -> int:
        """Width available for message content, excluding sidebar."""
        sidebar = 42 if self.sidebar_visible else 0
        return max(20, self.terminal_width - sidebar - 4)  # 4 = border padding

    def add_user_message(self, text: str) -> None:
        self.messages.append(Message(role="user", content=text))

    def start_assistant_turn(self) -> None:
        msg = Message(
            role="assistant", content="", parts=[],
            agent_mode=self.agent_mode.value,
            model=self.model,
            timestamp=time.time(),
        )
        self.messages.append(msg)
        self.streaming = True
        self.pending_text = ""
        self.pending_tool_calls = []
        self.tool_results = []
        self.turn_active = True

    def append_text(self, delta: str) -> None:
        self.pending_text += delta
        # Auto-scroll: keep view at bottom during streaming
        self.scroll_offset = 0

    def append_tool_call(self, name: str, args: str = "") -> None:
        call = ToolCallPart(name=name, args_preview=args, icon="$")
        self.pending_tool_calls.append(call)

    def append_tool_result(self, name: str, result: str, icon: str) -> None:
        part = ToolResultPart(name=name, icon=icon, result=result)
        self.tool_results.append(part)
        # Mark matching pending call as completed
        for tc in self.pending_tool_calls:
            if tc.name == name and not tc.completed:
                tc.completed = True
                break

    def finalize_turn(self, final_content: str) -> None:
        """Called when streaming completes.  Assembles parts into the message."""
        if not self.messages or self.messages[-1].role != "assistant":
            return
        msg = self.messages[-1]
        parts: list[Part] = []
        if self.pending_text.strip():
            parts.append(TextPart(text=self.pending_text))
        if final_content.strip():
            parts.append(TextPart(text=final_content))
        parts.extend(self.pending_tool_calls)
        parts.extend(self.tool_results)
        msg.parts = parts
        msg.content = final_content
        msg.duration = time.time() - msg.timestamp
        self.streaming = False
        self.turn_active = False
        self.pending_text = ""
        self.pending_tool_calls = []
        self.tool_results = []
        self.scroll_offset = 0  # stay at bottom after response

    def toggle_sidebar(self) -> None:
        self.sidebar_visible = not self.sidebar_visible

    def toggle_timestamps(self) -> None:
        self.show_timestamps = not self.show_timestamps

    def toggle_expanded(self, tool_id: str) -> None:
        if tool_id in self.expanded_tools:
            self.expanded_tools.discard(tool_id)
        else:
            self.expanded_tools.add(tool_id)

    def set_dialog(self, dialog: Dialog | None) -> None:
        self.dialog = dialog

    def compact_messages(self, summary: str) -> None:
        """Compact old messages into a single summary divider (C6)."""
        if len(self.messages) < 4:
            return
        self.compacted_count += 1
        self.compacted_messages.append(summary)
        # Keep only the last 2 messages (usually one user + one assistant)
        self.messages = self.messages[-2:]
