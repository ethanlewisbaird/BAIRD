# OpenCode TUI Parity — Full-Frame Re-Render Architecture

## Architecture

Instead of sequential `console.print()` (current), the entire UI is a **single re-renderable tree** driven by a **central state object** rendered inside `rich.Live`:

```
State (UIState) → render(state) → Rich Renderable → Live.display()
```

- Every frame re-renders the full layout from state
- Nothing is persistent; everything is derived from the state dict
- `Live(renderable, refresh_per_second=24)` drives animation
- Keyboard events mutate state and trigger `Live.update()`

## What Rich CAN do (corrected assumptions)

| Feature | Rich mechanism | Matches OpenCode? |
|---|---|---|
| Persistent layout | `rich.Layout` inside `Live` | Yes — full layout every frame |
| Sticky header/footer | Layout with fixed-size rows | Yes |
| Sidebar | Layout split column | Yes |
| Message area | Custom `Renderable` with viewport clipping | Yes |
| Scrollbar | Custom renderable or `Panel` with scroll indicator | Close |
| Streaming text | Append to state, re-render each chunk | Yes |
| Spinner animation | Timer updates spinner_frame in state → re-render | Yes |
| Tool expand/collapse | State-driven toggles | Yes |
| Dialog overlays | Overlay `Renderable` on top of layout | Yes |
| Resize handling | Terminal dimensions checked each render | Yes |
| Virtual scrolling | Viewport offset + visible range calculation | Yes |
| Markdown rendering | `rich.markdown.Markdown` | Close |
| Keyboard interaction | `prompt_toolkit` in same process mutates state | Yes |
| Mouse interaction | NOT supported by Rich | Limitation |

## Implementation Plan

### Phase A — Foundation

| # | Task | Description |
|---|---|---|
| A1 | `UIState` dataclass | Single source of truth for all UI |
| A2 | `LiveLayout` renderable | Custom `__rich_console__` that renders full UI from state |
| A3 | Header renderer | Sticky top bar: `┃ {badge}  {project} │ {model}` |
| A4 | Status bar renderer | Sticky bottom bar: `│ model=... turns=... cost=...` |
| A5 | Messages area renderer | Scrollable viewport with `Group` of message renderables |
| A6 | User message renderer | Left border `┃` in agent color, `backgroundPanel` bg |
| A7 | Assistant message renderer | Text content with `paddingLeft=3` |
| A8 | Tool block renderer | InlineTool + BlockTool (border-left + bg) |
| A9 | Sidebar renderer | Right panel, 42-char, toggled by state |
| A10 | Dialog overlay | Centered overlay with backdrop |
| A11 | Spinner frame | `SPINNER_FRAMES` cycling at state level |

### Phase B — Live Loop

| # | Task | Description |
|---|---|---|
| B1 | `run_live_tui()` | Main loop with `Live(render(state))` + `prompt_toolkit` |
| B2 | State mutation from keyboard | Arrow keys → scroll offset, Tab → mode toggle |
| B3 | Streaming → state append | `on_chunk` appends to state, triggers `Live.update()` |
| B4 | Tool events → state update | `on_tool_event` updates tool result state |
| B5 | Dialog key dispatch | Modal state routes keys to dialog handlers |

### Phase C — Parity Details

| # | Task | Description |
|---|---|---|
| C1 | Scrollbar | Vertical bar indicator in message area |
| C2 | Timestamps | Optionally show on messages |
| C3 | Expand/collapse tool output | Press key to toggle long output |
| C4 | Resize handling | `shutil.get_terminal_size()` per frame |
| C5 | Reasoning part | Dimmed text for thinking blocks |
| C6 | Compaction divider | "Compaction" line when history is compressed |
| C7 | Agent colour per message | Messages coloured by which agent generated them |
| C8 | Footer agent info | `▣ BUILD · model · duration` per assistant message |

## State Model

```python
@dataclass
class UIState:
    # Layout
    terminal_width: int
    terminal_height: int
    sidebar_visible: bool

    # Messages
    messages: list[Message]          # all conversation turns
    scroll_offset: int               # which message is at the top
    show_timestamps: bool

    # Streaming  
    streaming: bool                  # currently receiving model output
    pending_text: str                # accumulated text for current turn
    pending_tools: list[ToolCall]    # tools seen in current turn
    tool_results: list[ToolResult]   # completed tool results

    # Mode
    agent_mode: AgentMode

    # Config
    model: str
    project_id: str
    host_id: str | None
    session_id: str
    stats: ReplStats
    branch: str | None
    repo_ctx: RepoContext | None

    # Dialog
    dialog: Dialog | None = None

    # Animation
    spinner_frame: int = 0           # cycled by timer

    # Prompt
    prompt_text: str = ""

    # Expand/collapse
    expanded_tools: set[str] = field(default_factory=set)
```

## Layout Tree

```
Live (full terminal)
└── Layout (full terminal dimensions)
    ├── Row 0 (height=1)          → Header
    ├── Row 1 (flex=1)            → Content area (split column)
    │   ├── Column 0 (flex=1)     → Messages (scrollable)
    │   │   ├── Message 0         → UserMessage / AssistantMessage
    │   │   ├── Message 1
    │   │   └── ...
    │   └── Column 1 (width=42)   → Sidebar (conditional)
    └── Row 2 (height=1)          → Status bar
```

## Keyboard Bindings (target)

| Key | OpenCode command | Implementation |
|---|---|---|
| Tab | Toggle BUILD/PLAN | Mutate `agent_mode`, re-render |
| PgUp | Scroll up | Decrease `scroll_offset` |
| PgDn | Scroll down | Increase `scroll_offset` |
| Ctrl+S | Toggle sidebar | Toggle `sidebar_visible` |
| Ctrl+T | Toggle timestamps | Toggle `show_timestamps` |
| / | Search/filter | (future) |
| Enter | Submit prompt | Start turn, set `streaming=True` |
| Ctrl+C | Interrupt/quit | Stop streaming or exit |
| Esc | Close dialog | Set `dialog = None` |
