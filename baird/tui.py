"""Rich Live + Layout TUI for `baird code`.

Wraps the existing REPL semantics in a persistent layout:

  ┌─ project=… host=… branch=… ──────────────────────────────┐
  │                                                          │
  │ conversation panel (scrolling)                           │
  │                                                          │
  ├─ tokens: …→…  cost: $…  turns: …  inbox: …  budget: $… ──┤
  └──────────────────────────────────────────────────────────┘
  > input prompt below

Input goes through Rich's `console.input()`, which knows how to pause the
Live display while reading. Diff approval is modal: a Panel pops up over
the conversation and reads a single y/n/e/q keystroke via `tui_keys.read_key`.

Multi-line input (`\"\"\"` blocks), `/`-slash commands, model swap, session
resume — all the existing REPL features carry over.

`--no-tui` on `baird code` falls back to the line REPL in repl.py.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .context_loader import RepoContext, render_context
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


@dataclass
class FormField:
    """One field in a `collect_form` form.

    `validator` returns an error message (string) if the value is invalid, or
    None when it's accepted. Validators run after each prompt; on error the
    user is re-prompted.
    """

    name: str
    prompt: str
    default: str | None = None
    required: bool = False
    validator: Callable[[str], str | None] | None = None


def _form_status_table(
    fields: list[FormField], known: dict[str, str]
) -> Table:
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
    """Render a single status panel for `fields`, then prompt once for any
    missing required field (or any field whose prompt is forced by the
    caller via a `None` known value with a default — we just fill the default).

    The flow is intentionally one-shot: if the caller already supplied a value
    inline (the `/`-command parsing path), we don't re-prompt. Only the gaps
    are asked about. Validators may re-prompt that one field until it passes.

    Returns the merged dict. Optional unset fields with no default are absent
    from the returned dict (callers should treat absence as "leave it alone").
    """
    known = dict(known or {})
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
    rendered = render_context(repo_ctx)
    system = _system_prompt(rendered)
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
        session = hub.find_or_create_session_for_task(
            task_id=f"repl-{config.project_id}",
            project_id=config.project_id,
            mode="code",
        )

    iterator: Iterable[str] | None = iter(inputs) if inputs is not None else None

    def _print(line: Text | str) -> None:
        console.print(line)

    def _maybe_input(prompt: str) -> str:
        if iterator is not None:
            try:
                return next(iterator)
            except StopIteration:
                raise EOFError
        return console.input(prompt)

    console.print(_render_header(repo_ctx, host_id, session, config))
    console.print(Text(
        f"baird code — {config.project_id} — session={session['id'][:8]}",
        style="green",
    ))
    console.print(Text("/help for commands, /exit to quit", style="dim"))

    try:
        while True:
            try:
                raw = _maybe_input("\n[bold cyan]user[/bold cyan]> ")
            except (EOFError, KeyboardInterrupt):
                console.print()
                break

            # Multi-line `"""` block support.
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
                # First: try the hub-first slash registry (see baird/slash.py).
                from .agent_tools import ToolEnv
                from .slash import SlashContext
                from .slash import try_dispatch as _try_slash

                slash_ctx = SlashContext(
                    hub=hub,
                    env=ToolEnv(hub=hub, project_id=config.project_id),
                    input_fn=_maybe_input,
                    console=console,
                    active_host=getattr(config, "_active_host", None),
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
                    continue

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

            console.print(Rule(style="dim"))
            streamed_any = False

            def _on_chunk(delta: str) -> None:
                nonlocal streamed_any
                streamed_any = True
                console.out(delta, end="", highlight=False)

            try:
                completion = _one_turn(
                    user_msg=line,
                    hub=hub,
                    model_client=model_client,
                    session_id=session["id"],
                    config=config,
                    system=system,
                    host_id=host_id,
                    on_chunk=_on_chunk,
                )
            except ModelError as e:
                _print(Text(f"model error: {e}", style="red"))
                continue
            if streamed_any:
                console.print()
            else:
                console.print(completion.content)
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


# ---------- header / status panels --------------------------------------


def _render_header(ctx: RepoContext, host_id, session, config: ReplConfig) -> Panel:
    project = ctx.project.id if ctx.project else "?"
    branch = ctx.branch or "?"
    host = host_id or ctx.host_id or "?"
    body = (
        f"[green]baird[/green]  project=[cyan]{project}[/cyan]  "
        f"host={host}  branch={branch}  model={config.model}"
    )
    return Panel(body, border_style="green", padding=(0, 1))


def _render_status(stats: ReplStats, config: ReplConfig, completion=None) -> Panel:
    last = ""
    if completion is not None:
        last = (
            f"  last: {completion.usage.input_tokens}→{completion.usage.output_tokens}"
            f" tok / ${completion.cost_usd:.4f}"
        )
    body = (
        f"turns: [cyan]{stats.turns}[/cyan]  "
        f"cost: [yellow]${stats.total_cost_usd:.4f}[/yellow]  "
        f"tokens: {stats.total_input_tokens}→{stats.total_output_tokens}"
        f"{last}"
    )
    return Panel(body, border_style="blue", padding=(0, 1))


# ---------- /model handler ----------------------------------------------


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
        print_(Text(f"model: {old} → {new_model}", style="yellow"))


# ---------- modal diff approval -----------------------------------------


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
            title=f"diff {i}/{len(blocks)}  —  apply? [y/N/e/q]",
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
    """Open `$EDITOR` (default vi) on the diff text and return the edited
    body, or an empty string if the user saved an empty file."""
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
