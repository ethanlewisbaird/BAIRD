"""OpenCode-style TUI for `baird code` — dark theme.

Ported visual patterns from opencode (https://github.com/anomalyco/opencode):

  • Part-based rendering: text parts, tool-call parts, tool-result parts
  • Compact header with left-border vertical bar
  • Live tool-call streaming with single-char icons
  • Tool results as bordered blocks (InlineTool / BlockTool idiom)
  • Status bar as a plain text line with model/cost/tokens
  • prompt_toolkit input with slash-command autocomplete
  • OpenCode dark palette (theme.py)

`--no-tui` falls back to the line REPL in repl.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style as PtStyle
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .agent_tools import AgentMode
from .context_loader import RepoContext
from .diff_apply import DiffApplyError, apply_diff_to_repo
from .memory_client import HubClient
from .model import ModelError, OpenRouterClient, top_openrouter_models
from .repl import (
    ReplConfig,
    ReplStats,
    _one_turn,
    _system_prompt,
    extract_diff_blocks,
)
from .self_improve import maybe_background_review

from .layout import render_from_state
from .theme import (
    BAR,
    OC,
)
from .tui_keys import read_key
from .uistate import UIState, Dialog, Message, TextPart, ToolCallPart, ToolResultPart

# ── prompt_toolkit autocomplete ──────────────────────────────────────

_LOCAL_DESCRIPTIONS: dict[str, str] = {
    "exit": "Exit the REPL",
    "context": "Show current context",
    "reset": "Start a new session",
    "cost": "Show token usage and cost",
    "no-diff": "Disable diff approval prompts",
    "model": "Switch model",
    "project": "List / switch projects",
    "sessions": "List recent sessions",
    "connect": "Connect an API provider (OpenRouter/OpenCode Zen)",
}

_HUB_DESCRIPTIONS: dict[str, str] = {
    "project new": "Create a new project on the hub",
    "project rename": "Rename a project",
    "project delete": "Delete a project (destructive)",
    "project locations": "List project locations on satellites",
    "project add-location": "Add a location to a project",
    "project enrich": "Probe satellite paths for project metadata",
    "project tree": "Show project hierarchy tree",
    "project siblings": "List sibling projects",
    "host add": "Enrol a satellite host",
    "host edit": "Edit satellite watch root",
    "env install": "Install environment on a satellite",
    "where": "Search satellite paths for a project",
    "run": "Run a command on a satellite",
    "audit-satellite": "Audit satellite directory for projects",
    "satellite enroll": "Enrol a new satellite",
    "satellite list": "List enrolled satellites",
    "satellite remove": "Remove a satellite from the registry",
    "mcp connect": "Connect an MCP server",
    "mcp disconnect": "Disconnect an MCP server",
}


def _all_descriptions() -> dict[str, str]:
    """Combined description map: local + hub-first commands."""
    d: dict[str, str] = dict(_LOCAL_DESCRIPTIONS)
    for k, v in _HUB_DESCRIPTIONS.items():
        d[k] = v
    return d


class SlashCompleter(Completer):
    """Autocomplete for slash commands and @-mentions."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        # Slash commands at start of input
        if text.startswith("/") and " " not in text:
            partial = text[1:].lower()
            for cmd, desc in _all_descriptions().items():
                if cmd.startswith(partial):
                    yield Completion(
                        f"/{cmd} ", start_position=-len(text),
                        display_meta=desc,
                    )
        # @-mentions (opencode-style subagent invocation)
        at_idx = text.rfind("@")
        if at_idx >= 0 and " " not in text[at_idx:]:
            partial = text[at_idx + 1:]
            for mention, desc in [("general", "Complex multi-step tasks"), ("explore", "Fast codebase exploration")]:
                if mention.startswith(partial):
                    yield Completion(
                        f"@{mention} ", start_position=-len(partial),
                        display_meta=desc,
                    )


def _make_pt_session(
    on_mode_switch: Callable[[], None] | None = None,
    on_viewport_action: Callable[[str], None] | None = None,
) -> PromptSession:
    """Create a prompt_toolkit session with history, autocomplete, and key bindings.

    Keybindings (opencode-inspired):
      Tab        — 4 spaces or toggle BUILD/PLAN
      Ctrl+S     — toggle sidebar
      Ctrl+T     — toggle timestamps
      Ctrl+E     — toggle expand/collapse on all tool results
      PgUp       — scroll up
      PgDn       — scroll down
    """
    hist_path = os.path.expanduser("~/.baird/repl_history")
    os.makedirs(os.path.dirname(hist_path), exist_ok=True)

    kb = KeyBindings()

    @kb.add("tab")
    def _on_tab(event):
        b = event.app.current_buffer
        if b and b.text.strip():
            b.insert_text("    ")
        elif on_mode_switch is not None:
            on_mode_switch()

    @kb.add("c-s")
    def _on_ctrl_s(event):
        if on_viewport_action is not None:
            on_viewport_action("sidebar")

    @kb.add("c-t")
    def _on_ctrl_t(event):
        if on_viewport_action is not None:
            on_viewport_action("timestamps")

    @kb.add("c-e")
    def _on_ctrl_e(event):
        if on_viewport_action is not None:
            on_viewport_action("expand_all")

    @kb.add("pageup")
    def _on_page_up(event):
        if on_viewport_action is not None:
            on_viewport_action("scroll_up")

    @kb.add("pagedown")
    def _on_page_down(event):
        if on_viewport_action is not None:
            on_viewport_action("scroll_down")

    return PromptSession(
        completer=SlashCompleter(),
        auto_suggest=AutoSuggestFromHistory(),
        history=FileHistory(hist_path),
        key_bindings=kb,
        style=PtStyle.from_dict({
            "completion-menu.completion": f"bg:{OC.backgroundElement} {OC.text}",
            "completion-menu.completion.current": f"bg:{OC.primary} {OC.background}",
            "completion-menu.meta.completion": f"bg:{OC.backgroundPanel} {OC.textMuted}",
        }),
    )


# ── opencode-style tool icons ─────────────────────────────────────────

def _tool_icon(name: str) -> str:
    """Map tool name to opencode-style icon."""
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


# ── form helpers ──────────────────────────────────────────────────────

class FormParseError(ValueError):
    pass


@dataclass
class FormField:
    name: str
    prompt: str
    default: str | None = None
    required: bool = False
    validator: Callable[[str], str | None] | None = None


def _form_status_table(fields: list[FormField], known: dict[str, str]) -> Table:
    t = Table(show_header=True, header_style="dim", box=None, pad_edge=False)
    t.add_column("field"); t.add_column("status"); t.add_column("value", overflow="fold")
    for f in fields:
        v = known.get(f.name)
        if v is not None and v != "":
            status = f"[{OC.success}]set[/{OC.success}]"
            value = str(v)
        elif f.default is not None:
            status = f"[{OC.info}]default[/{OC.info}]"
            value = f.default
        elif f.required:
            status = f"[{OC.error}]missing[/{OC.error}]"
            value = ""
        else:
            status = f"[{OC.textMuted}]optional[/{OC.textMuted}]"
            value = ""
        t.add_row(f.name, status, value)
    return t


def collect_form_values(
    fields: list[FormField],
    known: dict[str, str] | None,
    *,
    input_fn: Callable[[str], str],
    console: Console | None = None,
) -> dict[str, str]:
    known = dict(known or {})
    for k, v in known.items():
        if isinstance(v, str) and v.startswith("--"):
            hint = v[2:].split()[0] if len(v) > 2 else ""
            suffix = f" — did you mean `--{hint} <value>`?" if hint else ""
            raise FormParseError(
                f"value for {k!r} starts with '--' ({v!r}) — "
                f"looks like an unparsed flag{suffix}"
            )
    if console is not None:
        console.print(Panel(_form_status_table(fields, known), border_style=OC.info, title="form"))

    out: dict[str, str] = {}
    for f in fields:
        v = known.get(f.name)
        if v is not None and v != "":
            out[f.name] = v
            continue
        if not f.required:
            if f.default is not None:
                out[f.name] = f.default
            continue
        while True:
            prompt = f.prompt
            if f.default is not None:
                prompt = f"{prompt} [{f.default}]"
            prompt = prompt + ": "
            raw = input_fn(prompt).strip()
            if raw == "" and f.default is not None:
                raw = f.default
            if raw == "":
                if console is not None:
                    console.print(Text(f"{f.name} is required", style=OC.error))
                continue
            if f.validator is not None:
                err = f.validator(raw)
                if err is not None:
                    if console is not None:
                        console.print(Text(err, style=OC.error))
                    continue
            out[f.name] = raw
            break
    return out


# ── main TUI loop ─────────────────────────────────────────────────────

def run_tui_repl(
    *,
    repo_ctx: RepoContext,
    hub: HubClient,
    model_client: OpenRouterClient,
    config: ReplConfig,
    console: Console,
    inputs: Iterable[str] | None = None,
    host_id: str | None = None,
    session_id: str | None = None,
) -> ReplStats:
    """Full-frame TUI — renders the entire UI from a single state object.

    Uses ``rich.Live`` for persistent full-frame rendering.
    Input is handled via ``prompt_toolkit`` between turns.
    """
    stats = ReplStats()
    diff_loop_active = config.diff_loop_enabled
    model_picker_cache: list[str] = []

    if session_id is not None:
        sessions = hub.list_sessions(project_id=config.project_id, limit=200)
        session = next((s for s in sessions if s["id"] == session_id), None)
        if session is None:
            raise RuntimeError(
                f"session {session_id} not found for project {config.project_id}"
            )
    else:
        session = hub.new_session(
            mode="code",
            task_id=f"repl-{config.project_id}",
            project_id=config.project_id,
        )

    from .context_loader import build_epoch_context, reconcile_context
    epoch = build_epoch_context(repo_ctx)

    from .agent_tools import AgentMode, ToolRegistry
    tool_registry = ToolRegistry()
    agent_mode = getattr(config, "_agent_mode", AgentMode.BUILD)
    system = _system_prompt(epoch.baseline, mode=agent_mode)

    iterator: Iterable[str] | None = iter(inputs) if inputs is not None else None
    _use_pt = iterator is None and os.isatty(0)

    # ── Build UI state ──
    state = UIState(
        terminal_width=80,
        terminal_height=24,
        model=config.model,
        project_id=config.project_id,
        host_id=host_id,
        session_id=session["id"][:8],
        agent_mode=agent_mode,
        stats=stats,
        project_display=repo_ctx.project.id if repo_ctx.project else config.project_id,
        host_display=host_id or repo_ctx.host_id or "?",
        branch_display=repo_ctx.branch or None,
    )

    # Store rendered context for /context command
    rendered: str = epoch.baseline

    def _switch_mode():
        nonlocal agent_mode, system, state
        agent_mode = agent_mode.toggle()
        system = _system_prompt(rendered, mode=agent_mode)
        state.agent_mode = agent_mode
        state.messages.append(Message(
            role="system",
            content=f"switched to {agent_mode.badge}",
            parts=[],
        ))

    def _on_viewport_action(action: str) -> None:
        if action == "sidebar":
            state.toggle_sidebar()
            _render()
        elif action == "timestamps":
            state.toggle_timestamps()
            _render()
        elif action == "expand_all":
            state.all_tools_expanded = not state.all_tools_expanded
            _render()
        elif action == "scroll_up":
            state.scroll_offset = min(state.scroll_offset + 4, len(state.messages) * 3)
            _render()
        elif action == "scroll_down":
            state.scroll_offset = max(state.scroll_offset - 4, 0)
            _render()

    pt_session: PromptSession | None = _make_pt_session(
        on_mode_switch=_switch_mode,
        on_viewport_action=_on_viewport_action,
    ) if _use_pt else None

    def _maybe_input(prompt: str) -> str:
        if iterator is not None:
            try:
                return next(iterator)
            except StopIteration:
                raise EOFError
        if pt_session is not None:
            return pt_session.prompt(prompt, vi_mode=False)
        return console.input(prompt)

    # ── Live loop ──
    from rich.live import Live as RichLive

    live: RichLive | None = None

    def _render():
        """Refresh the Live display from current state."""
        if live is not None:
            try:
                live.update(render_from_state(state))
            except Exception:
                pass

    def _print(line: Text | str) -> None:
        """Fallback print for non-Live output (slash command results, etc.)."""
        m = Text(line) if isinstance(line, str) else line
        state.messages.append(Message(role="system", content=m.plain, parts=[]))
        _render()

    try:
        with RichLive(
            render_from_state(state),
            screen=_use_pt,
            refresh_per_second=4,
        ) as lv:
            live = lv
            _render()

            while True:
                # ── Dialog key dispatch (B5) ──
                if state.dialog is not None:
                    _render()
                    try:
                        if _use_pt:
                            dlg_key = _maybe_input(f"{BAR}  ")
                        else:
                            dlg_key = _maybe_input("▸ ")
                    except (EOFError, KeyboardInterrupt):
                        break
                    dlg_key = dlg_key.strip().lower()
                    if dlg_key == "q" or dlg_key == "\x1b":
                        state.dialog = None
                        _render()
                        continue
                    if dlg_key.isdigit() and state.dialog.choices:
                        idx = int(dlg_key) - 1
                        if 0 <= idx < len(state.dialog.choices):
                            state.dialog.result = state.dialog.choices[idx]
                            state.dialog = None
                            _render()
                            continue
                    continue

                # ── Get user input (pause Live, use prompt_toolkit, resume) ──
                state.prompt_text = ""
                _render()
                try:
                    if _use_pt:
                        raw = _maybe_input(f"{BAR}  ")
                    else:
                        raw = _maybe_input("▸ ")
                except (EOFError, KeyboardInterrupt):
                    break

                if raw.strip() == '"""':
                    buf: list[str] = []
                    while True:
                        try:
                            nxt = _maybe_input("... ")
                        except (EOFError, KeyboardInterrupt):
                            break
                        if nxt.strip() == '"""':
                            break
                        buf.append(nxt.rstrip("\n"))
                    raw = "\n".join(buf)

                line = raw.strip()
                if not line:
                    continue

                # ── Slash commands ──
                if line.startswith("/"):
                    from .agent_tools import ToolEnv
                    from .slash import SlashContext
                    from .slash import try_dispatch as _try_slash

                    slash_ctx = SlashContext(
                        hub=hub,
                        env=ToolEnv(hub=hub, project_id=config.project_id),
                        input_fn=_maybe_input,
                        console=console,
                        active_host=getattr(config, "_active_host", None),
                        tool_registry=tool_registry,
                    )
                    slash_res = _try_slash(line[1:], slash_ctx)
                    if slash_res is not None:
                        if slash_res.output:
                            ok_s = OC.success if slash_res.ok else OC.error
                            state.messages.append(Message(
                                role="system",
                                content=slash_res.output,
                                parts=[],
                            ))
                            _render()
                        if slash_res.active_host:
                            config._active_host = slash_res.active_host  # type: ignore[attr-defined]
                        if slash_res.switch_to_project:
                            from .repl import _switch_project

                            swapped = _switch_project(
                                slash_res.switch_to_project, hub, config, host_id, console
                            )
                            if swapped[0] is not None:
                                rendered, system, repo_ctx, session = swapped
                                state.project_id = config.project_id
                                state.session_id = session["id"][:8]
                                state.project_display = repo_ctx.project.id if repo_ctx.project else config.project_id
                                _render()
                        if slash_res.next_user_prompt:
                            line = slash_res.next_user_prompt
                        else:
                            continue

                    handed_off = False
                    if slash_res is not None and slash_res.next_user_prompt:
                        handed_off = True
                    if not handed_off:
                        cmd = line[1:].split()[0].lower()
                        if cmd in {"exit", "quit"}:
                            break
                        if cmd == "context":
                            state.messages.append(Message(role="system", content=rendered, parts=[]))
                            _render()
                            continue
                        if cmd == "reset":
                            session = hub.new_session(
                                mode="code",
                                project_id=config.project_id,
                                task_id=f"repl-{config.project_id}",
                            )
                            state.session_id = session["id"][:8]
                            state.messages.clear()
                            _render()
                            continue
                        if cmd == "cost":
                            state.messages.append(Message(role="system", content=(
                                f"turns={stats.turns}  cost=${stats.total_cost_usd:.4f}  "
                                f"tokens={stats.total_input_tokens}→{stats.total_output_tokens}"
                            ), parts=[]))
                            _render()
                            continue
                        if cmd in {"no-diff", "nodiff"}:
                            diff_loop_active = False
                            state.messages.append(Message(role="system", content="diff prompts disabled for this session", parts=[]))
                            _render()
                            continue
                        if cmd == "model":
                            _handle_model_cmd(line, config, model_picker_cache, console, _print)
                            state.model = config.model
                            _render()
                            continue
                        if cmd == "project":
                            from .context_loader import lite_repo_context
                            from .project_yaml import ProjectYaml

                            parts = line.split()
                            if len(parts) == 1:
                                rows = hub.list_projects()
                                if not rows:
                                    state.messages.append(Message(role="system", content="no projects on the hub", parts=[]))
                                else:
                                    for r in rows:
                                        marker = "*" if r["id"] == config.project_id else " "
                                        state.messages.append(Message(role="system", content=f" {marker} {r['id']}  {r.get('name','')}", parts=[]))
                                    state.messages.append(Message(role="system", content="switch: /project <id>   create: /project new <id> [name]", parts=[]))
                                _render()
                                continue
                            sub = parts[1]
                            if sub == "list":
                                rows = hub.list_projects()
                                if not rows:
                                    state.messages.append(Message(role="system", content="no projects on the hub", parts=[]))
                                else:
                                    for r in rows:
                                        marker = "*" if r["id"] == config.project_id else " "
                                        state.messages.append(Message(role="system", content=f" {marker} {r['id']}  {r.get('name','')}", parts=[]))
                                    state.messages.append(Message(role="system", content="switch: /project <id>   create: /project new <id> [name]", parts=[]))
                                _render()
                                continue
                            if sub == "new":
                                if len(parts) < 3:
                                    state.messages.append(Message(role="system", content="usage: /project new <id> [name]", parts=[]))
                                    _render()
                                    continue
                                new_id = parts[2]
                                new_name = " ".join(parts[3:]) if len(parts) > 3 else new_id
                                try:
                                    hub.upsert_project(id=new_id, name=new_name)
                                except Exception as e:
                                    state.messages.append(Message(role="system", content=f"create failed: {e}", parts=[]))
                                    _render()
                                    continue
                                state.messages.append(Message(role="system", content=f"created project {new_id}", parts=[]))
                                _render()
                                target_id = new_id
                            else:
                                target_id = sub
                            try:
                                proj_row = hub.get_project(target_id)
                            except Exception as e:
                                state.messages.append(Message(role="system", content=f"project '{target_id}' not on hub: {e}", parts=[]))
                                _render()
                                continue
                            py = ProjectYaml(
                                id=proj_row["id"],
                                name=proj_row.get("name") or proj_row["id"],
                                github=proj_row.get("github"),
                                context=proj_row.get("context"),
                                parent_id=proj_row.get("parent_id")
                                or (proj_row.get("config") or {}).get("parent_id"),
                            )
                            repo_ctx = lite_repo_context(py, hub=hub, host_id=host_id)
                            rendered = render_context(repo_ctx)
                            system = _system_prompt(rendered, mode=agent_mode)
                            config.project_id = target_id
                            config.project_root = None
                            session = hub.find_or_create_session_for_task(
                                task_id=f"repl-{target_id}",
                                project_id=target_id,
                                mode="code",
                            )
                            state.project_id = target_id
                            state.session_id = session["id"][:8]
                            state.project_display = target_id
                            state.messages.append(Message(role="system", content=f"switched to project {target_id}  session={session['id'][:8]}", parts=[]))
                            _render()
                            continue
                        if cmd == "sessions":
                            rows = hub.list_sessions(project_id=config.project_id, limit=20)
                            if not rows:
                                state.messages.append(Message(role="system", content="no prior sessions for this project", parts=[]))
                            else:
                                state.messages.append(Message(role="system", content=f"sessions for {config.project_id}", parts=[]))
                                for r in rows:
                                    marker = "*" if r["id"] == session["id"] else " "
                                    state.messages.append(Message(role="system", content=f" {marker} {r['id'][:8]}  {r.get('mode','?')}  started={r.get('started_at','')[:19]}", parts=[]))
                            _render()
                            continue
                        if cmd == "help":
                            from .slash import commands as _slash_cmds

                            text = ("/exit  /context  /reset  /cost  /model [id]  "
                                    "/sessions  /project [id|new <id>]  /no-diff  /connect")
                            state.messages.append(Message(role="system", content=text, parts=[]))
                            state.messages.append(Message(role="system", content="hub-first: " + "  ".join(f"/{c}" for c in _slash_cmds()), parts=[]))
                            _render()
                            continue
                        state.messages.append(Message(role="system", content=f"unknown command: /{cmd} (try /help)", parts=[]))
                        _render()
                        continue

                # ── User message ──
                state.add_user_message(line)
                _render()

                # ── Context reconciliation ──
                if config.project_root is not None:
                    try:
                        from .context_loader import (
                            _build_tree,
                            _git_log_oneline,
                            _git_status,
                        )
                        fresh = RepoContext(
                            project=repo_ctx.project, project_root=config.project_root,
                            branch=repo_ctx.branch,
                            git_log_lines=_git_log_oneline(config.project_root, n=10),
                            git_status=_git_status(config.project_root),
                            tree=_build_tree(config.project_root),
                            relevant_files=repo_ctx.relevant_files,
                            decisions=repo_ctx.decisions,
                            action_summaries=repo_ctx.action_summaries,
                            rules_summary=repo_ctx.rules_summary,
                            host_id=repo_ctx.host_id,
                            locations=repo_ctx.locations,
                            parent=repo_ctx.parent,
                        )
                        updates = reconcile_context(epoch, fresh)
                        for key, content in updates:
                            msg_text = f"[context update: {key}]\n{content}"
                            hub.append_message(session["id"], role="system", content=msg_text)
                    except Exception:
                        pass

                # ── Model turn ──
                state.start_assistant_turn()
                _render()

                seen_tool_names: set[str] = set()

                def _on_chunk(delta: str) -> None:
                    state.spinner_frame += 1
                    if delta.startswith('{"tool_calls":'):
                        try:
                            tc_list = json.loads(delta).get("tool_calls", [])
                            for tc in tc_list:
                                fn = tc.get("function", tc)
                                name = fn.get("name", "?")
                                if name not in seen_tool_names:
                                    seen_tool_names.add(name)
                                    args_raw = fn.get("arguments", {})
                                    args_str = json.dumps(args_raw) if isinstance(args_raw, dict) else str(args_raw)
                                    icon = _tool_icon(name)
                                    state.append_tool_call(name, args_str[:100])
                                    _render()
                        except Exception:
                            pass
                    else:
                        state.append_text(delta)
                        # Re-render on every chunk for responsive typing feedback
                        _render()

                def _extract_tool_name(detail: str, event: str) -> str:
                    if event == "call":
                        return detail.split("(")[0]
                    if event == "result":
                        return detail.split(":")[0]
                    if event == "blocked":
                        if detail.startswith("unknown tool: "):
                            return detail[len("unknown tool: "):]
                        return detail.split(":")[0]
                    return "tool"

                def _on_tool_event(event: str, detail: str) -> None:
                    if event == "result" and detail:
                        name = _extract_tool_name(detail, event)
                        icon = _tool_icon(name)
                        # Strip leading "name: " prefix
                        content = detail.strip()
                        if ":" in content.splitlines()[0] if content else False:
                            _, _, rest = content.partition(": ")
                            content = rest
                        state.append_tool_result(name, content, icon)
                        _render()
                    elif event == "blocked":
                        state.messages.append(Message(role="system", content=f"blocked: {detail[:80]}", parts=[]))
                        _render()
                    elif event == "files" and detail:
                        state.messages.append(Message(role="system", content=f"files changed: {detail[:200]}", parts=[]))
                        _render()

                try:
                    completion = _one_turn(
                        user_msg=line,
                        hub=hub,
                        model_client=model_client,
                        session_id=session["id"],
                        config=config,
                        system=system,
                        host_id=host_id,
                        tool_registry=tool_registry,
                        agent_mode=agent_mode,
                        on_chunk=_on_chunk,
                        on_tool_event=_on_tool_event,
                    )
                except ModelError as e:
                    state.messages.append(Message(role="system", content=f"model error: {e}", parts=[]))
                    _render()
                    continue

                # ── Finalize turn ──
                state.finalize_turn(completion.content or "")
                stats.turns += 1
                stats.total_cost_usd += completion.cost_usd
                stats.total_input_tokens += completion.usage.input_tokens
                stats.total_output_tokens += completion.usage.output_tokens
                state.stats = stats
                state.spinner_frame = 0
                _render()

                # ── Background review ──
                if stats.turns % 4 == 0:
                    try:
                        msgs = hub.get_messages(session["id"], limit=10)
                        maybe_background_review(model_client, msgs, config, console)
                    except Exception:
                        pass

                # ── Diff loop ──
                if diff_loop_active and config.project_root is not None:
                    _handle_diff_blocks_tui(
                        completion.content or "",
                        project_root=config.project_root,
                        console=console,
                        print_=_print,
                    )

    finally:
        pass

    return stats


# ── /model handler ───────────────────────────────────────────────────


def _handle_model_cmd(
    line: str,
    config: ReplConfig,
    model_picker_cache: list[str],
    console: Console,
    print_,
) -> None:
    parts = line.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) == 2 else ""
    if not arg:
        print_(Text(f"current model: {config.model}", style=OC.textMuted))
        try:
            picks = top_openrouter_models(n=20)
            model_picker_cache.clear()
            model_picker_cache.extend(m.get("id", "") for m in picks)
            for i, m in enumerate(picks, 1):
                print_(Text(f"  {i:>2}. {m.get('id','')}", style=OC.secondary))
            print_(Text("usage: /model <number> or /model <full-id>", style=OC.textMuted))
        except Exception as e:
            print_(Text(f"could not fetch model list ({e})", style=OC.warning))
    else:
        new_model: str | None = None
        if arg.isdigit() and model_picker_cache:
            idx = int(arg)
            if 1 <= idx <= len(model_picker_cache):
                new_model = model_picker_cache[idx - 1]
        if not new_model:
            new_model = arg
        old = config.model
        config.model = new_model
        print_(Text(f"model: {old} \u2192 {new_model}", style=OC.info))


# ── modal diff approval ──────────────────────────────────────────────


def _handle_diff_blocks_tui(
    content: str,
    *,
    project_root: Path,
    console: Console,
    print_,
) -> None:
    blocks = extract_diff_blocks(content)
    if not blocks:
        return
    for i, diff in enumerate(blocks, 1):
        syntax = Syntax(diff, "diff", theme="monokai", line_numbers=False)
        modal = Panel(
            syntax,
            title=f"diff {i}/{len(blocks)}  \u2014  apply? [y/N/e/q]",
            border_style=OC.warning,
        )
        console.print(modal)
        choice = read_key("ynqe")
        if choice == "q":
            print_(Text("exiting diff loop", style=OC.warning))
            return
        if choice == "e":
            diff = _edit_diff(diff)
            if not diff:
                print_(Text("edit cancelled", style=OC.textMuted))
                continue
        if choice not in {"y", "e"}:
            print_(Text("skipped", style=OC.textMuted))
            continue
        try:
            result = apply_diff_to_repo(
                repo=project_root,
                diff_text=diff,
                commit_message=f"baird: apply REPL-proposed diff {i}",
                action_id=f"repl-{uuid.uuid4().hex[:8]}",
            )
        except DiffApplyError as e:
            print_(Text(f"apply failed: {e}", style=OC.error))
            continue
        print_(Text(
            f"applied {result.commit_sha[:12]} ({len(result.files_changed)} file(s))",
            style=OC.success,
        ))


def _edit_diff(diff: str) -> str:
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as f:
        f.write(diff)
        path = f.name
    try:
        subprocess.run([editor, path], check=False)
        with open(path) as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
