"""OpenCode-style TUI for `baird code` — British-flag colour theme.

Ported visual patterns from opencode (https://github.com/anomalyco/opencode):

  • Part-based rendering: text parts, tool-call parts, tool-result parts
  • Compact header with left-border vertical bar
  • Live tool-call streaming with single-char icons
  • Tool results as bordered blocks (InlineTool / BlockTool idiom)
  • Status bar as an inline Rule with model/cost/tokens
  • prompt_toolkit input with slash-command autocomplete

Colour theme (Union Jack):
  • Red    #C8102E — actions, prompts, emphasis
  • Blue   #012169 — headers, borders, info
  • Light  #1D70B8 — links, secondary
  • White  #FFFFFF — text
  • Navy   #0B1D3A — background

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
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style as PtStyle
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .context_loader import RepoContext
from .diff_apply import DiffApplyError, apply_diff_to_repo
from .memory_client import HubClient
from .model import ModelError, OpenRouterClient, top_openrouter_models
from .repl import (
    HISTORY_TURN_CAP,
    ReplConfig,
    ReplStats,
    _one_turn,
    _system_prompt,
    extract_diff_blocks,
)
from .tui_keys import read_key

# British-flag colour palette (Union Jack)
BRIT_RED = "#C8102E"
BRIT_BLUE = "#012169"
BRIT_LIGHT_BLUE = "#1D70B8"
BRIT_WHITE = "#FFFFFF"
BRIT_NAVY = "#0B1D3A"
BRIT_DIM = "#6B7C93"
BRIT_GREEN = "#228B22"

# ── opencode-style rendering helpers ──────────────────────────────────

BAR = "\u2502"
BAR_THICK = "\u2503"


# ── prompt_toolkit autocomplete ──────────────────────────────────────

_LOCAL_COMMANDS = [
    "exit", "context", "reset", "cost", "no-diff",
    "model", "project", "sessions",
]


def _all_slash_commands() -> list[str]:
    """Combined list of local commands + hub-first slash commands (matching /help)."""
    from .slash import commands as slash_commands
    return _LOCAL_COMMANDS + slash_commands()


class SlashCompleter(Completer):
    """Autocomplete for slash commands and @-mentions."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        # Slash commands at start of input
        if text.startswith("/") and " " not in text:
            partial = text[1:].lower()
            for cmd in _all_slash_commands():
                if cmd.startswith(partial):
                    yield Completion(f"/{cmd} ", start_position=-len(text))
        # @-mentions
        at_idx = text.rfind("@")
        if at_idx >= 0 and " " not in text[at_idx:]:
            partial = text[at_idx + 1:]
            yield Completion(f"@{partial} ", start_position=-len(partial))


def _make_pt_session() -> PromptSession:
    """Create a prompt_toolkit session with history and autocomplete."""
    hist_path = os.path.expanduser("~/.baird/repl_history")
    os.makedirs(os.path.dirname(hist_path), exist_ok=True)
    return PromptSession(
        completer=SlashCompleter(),
        auto_suggest=AutoSuggestFromHistory(),
        history=FileHistory(hist_path),
        style=PtStyle.from_dict({
            "completion-menu.completion": "bg:#012169 #ffffff",
            "completion-menu.completion.current": "bg:#C8102E #ffffff",
            "completion-menu.meta.completion": "bg:#1D70B8 #ffffff",
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


def _tool_style(name: str) -> str:
    """Style tag for a tool call line."""
    shell_like = {"run_on", "read_remote", "write_remote", "apply_diff_remote"}
    if name in shell_like:
        return f"bold {BRIT_RED}"
    return f"{BRIT_LIGHT_BLUE}"


# ── opencode-style header / status ────────────────────────────────────

def _render_header(ctx: RepoContext, host_id, session, config: ReplConfig) -> Text:
    """Compact status line with left-border vertical bar — opencode style."""
    project = ctx.project.id if ctx.project else "?"
    branch = ctx.branch or "?"
    host = host_id or ctx.host_id or "?"
    return Text.assemble(
        (f"{BAR_THICK} baird  ", f"bold {BRIT_RED}"),
        (f"project={project}  host={host}  branch={branch}  ", BRIT_WHITE),
        (f"model={config.model}", BRIT_DIM),
    )


def _render_header_compact(host_id, session, config: ReplConfig) -> Text:
    """Short header when no RepoContext is available."""
    return Text.assemble(
        (f"{BAR_THICK} baird  ", f"bold {BRIT_RED}"),
        (f"project={config.project_id}  host={host_id or '?'}  ", BRIT_WHITE),
        (f"model={config.model}", BRIT_DIM),
    )


def _render_status(stats: ReplStats, config: ReplConfig, completion=None) -> Rule:
    """Status bar as an inline Rule — opencode footer-bar idiom."""
    parts: list[tuple[str, str]] = [
        (f"  model={config.model}  ", BRIT_DIM),
        (f"turns={stats.turns}  ", BRIT_LIGHT_BLUE),
        (f"cost=${stats.total_cost_usd:.4f}  ", BRIT_RED),
        (f"tokens={stats.total_input_tokens}\u2192{stats.total_output_tokens}", BRIT_WHITE),
    ]
    if completion is not None:
        parts.append((f"  last: {completion.usage.input_tokens}\u2192{completion.usage.output_tokens}", BRIT_DIM))
        parts.append((f"  ${completion.cost_usd:.4f}", BRIT_DIM))
    text = Text.assemble(*parts)
    return Rule(text, style=BRIT_BLUE)


def _tool_result_block(tool_name: str, result_text: str, icon: str, style: str) -> Panel:
    """Render a tool result as a bordered block.

    Shows the tool name/icon as panel title and result content inside
    a rounded border — matches opencode's BlockTool idiom.
    """
    content = result_text.strip()
    # Strip the leading "tool_name: " prefix that _dispatch_tool_call prepends
    if ":" in content.splitlines()[0] if content else False:
        prefix, _, rest = content.partition(": ")
        content = rest
    if not content:
        content = "(empty result)"
    return Panel(
        Text(content, style=BRIT_WHITE),
        title=f"{icon} {tool_name}",
        title_align="left",
        border_style=style,
        padding=(0, 1),
    )


# ── form helpers (unchanged, opencode doesn't have forms) ─────────────

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
            status = "[green]set[/green]"
            value = str(v)
        elif f.default is not None:
            status = "[cyan]default[/cyan]"
            value = f.default
        elif f.required:
            status = "[red]missing[/red]"
            value = ""
        else:
            status = "[dim]optional[/dim]"
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
        console.print(Panel(_form_status_table(fields, known), border_style="cyan", title="form"))

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
                    console.print(Text(f"{f.name} is required", style="red"))
                continue
            if f.validator is not None:
                err = f.validator(raw)
                if err is not None:
                    if console is not None:
                        console.print(Text(err, style="red"))
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
    """TUI variant of `run_repl`. Same semantics, persistent layout."""
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
    system = _system_prompt(epoch.baseline)

    from .agent_tools import AgentMode, ToolRegistry
    tool_registry = ToolRegistry()
    agent_mode = getattr(config, "_agent_mode", AgentMode.BUILD)

    iterator: Iterable[str] | None = iter(inputs) if inputs is not None else None
    _use_pt = iterator is None and os.isatty(0)
    pt_session: PromptSession | None = _make_pt_session() if _use_pt else None

    # Store rendered context for /context command
    rendered: str = epoch.baseline

    def _print(line: Text | str) -> None:
        console.print(line)

    def _maybe_input(prompt: str) -> str:
        if iterator is not None:
            try:
                return next(iterator)
            except StopIteration:
                raise EOFError
        if pt_session is not None:
            return pt_session.prompt(prompt, vi_mode=True)
        return console.input(prompt)

    console.print(_render_header(repo_ctx, host_id, session, config))
    console.print(Text(
        f"{BAR_THICK} {session['id'][:8]}", style=BRIT_DIM
    ))

    try:
        while True:
            try:
                if _use_pt:
                    raw = _maybe_input(f"\n{BAR} ")
                else:
                    raw = _maybe_input(f"\n[{BRIT_RED}]{BAR}[/{BRIT_RED}] ")
            except (EOFError, KeyboardInterrupt):
                console.print()
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
                        style = "green" if slash_res.ok else "red"
                        _print(Text(slash_res.output, style=style))
                    if slash_res.active_host:
                        config._active_host = slash_res.active_host  # type: ignore[attr-defined]
                    if slash_res.switch_to_project:
                        from .repl import _switch_project

                        swapped = _switch_project(
                            slash_res.switch_to_project, hub, config, host_id, console
                        )
                        if swapped[0] is not None:
                            rendered, system, repo_ctx, session = swapped
                            console.print(_render_header(repo_ctx, host_id, session, config))
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
                        _print(Text(rendered, style="dim"))
                        continue
                    if cmd == "reset":
                        session = hub.new_session(
                            mode="code",
                            project_id=config.project_id,
                            task_id=f"repl-{config.project_id}",
                        )
                        _print(Text(f"new session {session['id'][:8]}", style="yellow"))
                        continue
                    if cmd == "cost":
                        _print(Text(
                            f"turns={stats.turns}  cost=${stats.total_cost_usd:.4f}  "
                            f"tokens={stats.total_input_tokens}→{stats.total_output_tokens}",
                            style="dim",
                        ))
                        continue
                    if cmd in {"no-diff", "nodiff"}:
                        diff_loop_active = False
                        _print(Text("diff prompts disabled for this session", style="yellow"))
                        continue
                    if cmd == "model":
                        _handle_model_cmd(line, config, model_picker_cache, console, _print)
                        continue
                    if cmd == "project":
                        from .context_loader import lite_repo_context
                        from .project_yaml import ProjectYaml

                        parts = line.split()
                        if len(parts) == 1:
                            rows = hub.list_projects()
                            if not rows:
                                _print(Text("no projects on the hub", style="dim"))
                            else:
                                for r in rows:
                                    marker = "*" if r["id"] == config.project_id else " "
                                    _print(Text(f" {marker} {r['id']}  {r.get('name','')}", style="cyan"))
                                _print(Text("switch: /project <id>   create: /project new <id> [name]", style="dim"))
                            continue
                        sub = parts[1]
                        if sub == "list":
                            rows = hub.list_projects()
                            if not rows:
                                _print(Text("no projects on the hub", style="dim"))
                            else:
                                for r in rows:
                                    marker = "*" if r["id"] == config.project_id else " "
                                    _print(Text(f" {marker} {r['id']}  {r.get('name','')}", style="cyan"))
                                _print(Text("switch: /project <id>   create: /project new <id> [name]", style="dim"))
                            continue
                        if sub == "new":
                            if len(parts) < 3:
                                _print(Text("usage: /project new <id> [name]", style="red"))
                                continue
                            new_id = parts[2]
                            new_name = " ".join(parts[3:]) if len(parts) > 3 else new_id
                            try:
                                hub.upsert_project(id=new_id, name=new_name)
                            except Exception as e:
                                _print(Text(f"create failed: {e}", style="red"))
                                continue
                            _print(Text(f"created project {new_id}", style="green"))
                            target_id = new_id
                        else:
                            target_id = sub
                        try:
                            proj_row = hub.get_project(target_id)
                        except Exception as e:
                            _print(Text(f"project '{target_id}' not on hub: {e}", style="red"))
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
                        system = _system_prompt(rendered)
                        config.project_id = target_id
                        config.project_root = None
                        session = hub.find_or_create_session_for_task(
                            task_id=f"repl-{target_id}",
                            project_id=target_id,
                            mode="code",
                        )
                        console.print(_render_header(repo_ctx, host_id, session, config))
                        _print(Text(
                            f"switched to project {target_id}  session={session['id'][:8]}",
                            style="yellow",
                        ))
                        continue
                    if cmd == "sessions":
                        rows = hub.list_sessions(project_id=config.project_id, limit=20)
                        if not rows:
                            _print(Text("no prior sessions for this project", style="dim"))
                        else:
                            _print(Text(f"sessions for {config.project_id}", style="dim"))
                            for r in rows:
                                marker = "*" if r["id"] == session["id"] else " "
                                _print(Text(
                                    f" {marker} {r['id'][:8]}  {r.get('mode','?')}  "
                                    f"started={r.get('started_at','')[:19]}",
                                    style="dim",
                                ))
                        continue
                    if cmd == "help":
                        from .slash import commands as _slash_cmds

                        _print(Text(
                            "/exit  /context  /reset  /cost  /model [id]  "
                            "/sessions  /project [id|new <id>]  /no-diff",
                            style="dim",
                        ))
                        _print(Text(
                            "hub-first: " + "  ".join(f"/{c}" for c in _slash_cmds()),
                            style="dim",
                        ))
                        continue
                    _print(Text(f"unknown command: /{cmd} (try /help)", style="red"))
                    continue

            # ── reset turn state ──
            seen_tool_names: set[str] = set()
            turn_text_parts: list[str] = []
            tool_spinner_idx = 0

            def _on_chunk(delta: str) -> None:
                nonlocal tool_spinner_idx
                # Tool calls arrive as JSON {"tool_calls": [...]}
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
                                style = _tool_style(name)
                                console.out(
                                    f"\n  {icon} {name}({args_str[:100]})",
                                    style=style, highlight=False,
                                )
                    except Exception:
                        pass
                else:
                    turn_text_parts.append(delta)
                    tool_spinner_idx += 1
                    if tool_spinner_idx % 80 == 0 and seen_tool_names:
                        console.out(".", style=BRIT_DIM, end="", highlight=False)

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
                    style = _tool_style(name)
                    block = _tool_result_block(name, detail, icon, style)
                    console.print(block)
                elif event == "blocked":
                    _print(Text(f"\u2502  blocked: {detail[:80]}", style=f"bold {BRIT_RED}"))
                elif event == "files" and detail:
                    _print(Text(f"\u2502  files changed: {detail[:200]}", style=BRIT_DIM))

            from .context_loader import render_context
            from .diff_apply import apply_diff_to_repo

            if config.project_root is not None:
                try:
                    from .context_loader import (
                        _git_log_oneline, _git_status, _build_tree,
                        GIT_LOG_SOURCE, GIT_STATUS_SOURCE, TREE_SOURCE,
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
                _print(Text(f"model error: {e}", style=BRIT_RED))
                continue

            turn_final_content = completion.content
            was_tools = len(seen_tool_names) > 0

            if turn_final_content:
                lines = turn_final_content.strip().splitlines()
                for ln in lines:
                    console.print(Text(ln, style=BRIT_WHITE))
            elif not was_tools and turn_text_parts:
                console.print(Text("".join(turn_text_parts).strip(), style=BRIT_WHITE))

            stats.turns += 1
            stats.total_cost_usd += completion.cost_usd
            stats.total_input_tokens += completion.usage.input_tokens
            stats.total_output_tokens += completion.usage.output_tokens
            console.print(_render_status(stats, config, completion))

            if diff_loop_active and config.project_root is not None:
                _handle_diff_blocks_tui(
                    completion.content,
                    project_root=config.project_root,
                    console=console,
                    print_=_print,
                )

        _print(Text(
            f"session={session['id'][:8]}  turns={stats.turns}  "
            f"total=${stats.total_cost_usd:.4f}",
            style="dim",
        ))
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
        print_(Text(f"current model: {config.model}", style="dim"))
        try:
            picks = top_openrouter_models(n=20)
            model_picker_cache.clear()
            model_picker_cache.extend(m.get("id", "") for m in picks)
            for i, m in enumerate(picks, 1):
                print_(Text(f"  {i:>2}. {m.get('id','')}", style="cyan"))
            print_(Text("usage: /model <number> or /model <full-id>", style="dim"))
        except Exception as e:
            print_(Text(f"could not fetch model list ({e})", style="yellow"))
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
        print_(Text(f"model: {old} \u2192 {new_model}", style="yellow"))


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
            border_style="yellow",
        )
        console.print(modal)
        choice = read_key("ynqe")
        if choice == "q":
            print_(Text("exiting diff loop", style="yellow"))
            return
        if choice == "e":
            diff = _edit_diff(diff)
            if not diff:
                print_(Text("edit cancelled", style="dim"))
                continue
        if choice not in {"y", "e"}:
            print_(Text("skipped", style="dim"))
            continue
        try:
            result = apply_diff_to_repo(
                repo=project_root,
                diff_text=diff,
                commit_message=f"baird: apply REPL-proposed diff {i}",
                action_id=f"repl-{uuid.uuid4().hex[:8]}",
            )
        except DiffApplyError as e:
            print_(Text(f"apply failed: {e}", style="red"))
            continue
        print_(Text(
            f"applied {result.commit_sha[:12]} ({len(result.files_changed)} file(s))",
            style="green",
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
