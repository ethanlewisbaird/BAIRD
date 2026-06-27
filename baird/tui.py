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
from pathlib import Path
from typing import Iterable, Optional

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

from .context_loader import RepoContext, render_context
from .diff_apply import DiffApplyError, apply_diff_to_repo
from .memory_client import HubClient
from .model import ModelError, OpenRouterClient
from .model import top_openrouter_models
from .repl import (
    HISTORY_TURN_CAP,
    ReplConfig,
    ReplStats,
    _one_turn,
    _system_prompt,
    extract_diff_blocks,
)
from .tui_keys import read_key


def run_tui_repl(
    *,
    repo_ctx: RepoContext,
    hub: HubClient,
    model_client: OpenRouterClient,
    config: ReplConfig,
    console: Console,
    inputs: Optional[Iterable[str]] = None,
    host_id: Optional[str] = None,
    session_id: Optional[str] = None,
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

    iterator: Optional[Iterable[str]] = iter(inputs) if inputs is not None else None

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
                    _print(Text(
                        "/exit  /context  /reset  /cost  /model [id]  "
                        "/sessions  /project [id|new <id>]  /no-diff",
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
        new_model: Optional[str] = None
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
