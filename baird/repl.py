"""Multi-turn coding REPL for `baird code` — Phase 4b + diff loop.

Each user line is a turn:
  1. Append user message to the persistent project Session (one per project).
  2. Open a per-turn Action (so budgets + history attribute cost correctly).
  3. Call the model with the recent message history + system prompt.
  4. Append assistant message; record cost on the action.
  5. Print the reply + per-turn cost/token footer.
  6. If the reply contains fenced ```diff blocks, prompt for per-block approval.

Special inputs start with `/`:
  /exit, /quit        — leave the REPL
  /context            — re-render the repo context block
  /reset              — start a fresh session (drops prior history)
  /cost               — show cumulative cost for this REPL invocation
  /model [id]         — show or change the OpenRouter model mid-session
  /no-diff            — skip diff prompting for the rest of the session
  /help               — list slash commands
"""

from __future__ import annotations

import re
import sys
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from .context_loader import RepoContext, render_context
from .diff_apply import DiffApplyError, apply_diff_to_repo
from .memory_client import HubClient
from .model import Completion, ModelError, OpenRouterClient


_DIFF_BLOCK_RE = re.compile(r"```(?:diff|patch)\s*\n(.*?)```", re.DOTALL)


def extract_diff_blocks(text: str) -> list[str]:
    """Pull any ```diff / ```patch fenced blocks out of `text`."""
    return [m.group(1) for m in _DIFF_BLOCK_RE.finditer(text or "")]


HISTORY_TURN_CAP = 20  # last N messages sent to the model on each turn


@dataclass
class ReplStats:
    turns: int = 0
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


@dataclass
class ReplConfig:
    project_id: str
    model: str = "openrouter/owl-alpha"
    max_tokens: int = 1024
    temperature: float = 0.2
    history_cap: int = HISTORY_TURN_CAP
    project_root: Path | None = None  # required for diff_apply
    diff_loop_enabled: bool = True


def _system_prompt(rendered_context: str) -> str:
    from .agent_tools import tool_catalogue_prompt

    return (
        "You are BAIRD, a bioinformatics research assistant. The active project's "
        "context follows. Be concise; when proposing code, give the change as a "
        "fenced unified diff so it can be reviewed and applied. For changes to "
        "hub-owned state (project locations, decisions, environment installs, "
        "satellite host.yaml) call the matching tool below instead of writing a "
        "diff.\n\n"
        + rendered_context
        + "\n\n"
        + tool_catalogue_prompt()
    )


def run_repl(
    *,
    repo_ctx: RepoContext,
    hub: HubClient,
    model_client: OpenRouterClient,
    config: ReplConfig,
    console: Console,
    input_fn: Callable[[str], str] = input,
    inputs: Iterable[str] | None = None,
    host_id: str | None = None,
    session_id: str | None = None,
) -> ReplStats:
    """Run the REPL until `/exit`, EOF, or the `inputs` iterable is exhausted.

    `inputs` is for tests — when supplied, lines are consumed from it instead
    of calling `input_fn`. EOF behaves like `/exit`.

    `session_id`, if provided, attaches to that specific session (re-loads its
    history) instead of the project's default `repl-<project_id>` session.
    """
    stats = ReplStats()
    rendered = render_context(repo_ctx)
    system = _system_prompt(rendered)
    # Local switch — user can toggle off mid-session via /no-diff.
    diff_loop_active = config.diff_loop_enabled
    model_picker_cache: list[str] = []

    if session_id is not None:
        sessions = hub.list_sessions(project_id=config.project_id, limit=200)
        session = next((s for s in sessions if s["id"] == session_id), None)
        if session is None:
            raise RuntimeError(f"session {session_id} not found for project {config.project_id}")
    else:
        session = hub.find_or_create_session_for_task(
            task_id=f"repl-{config.project_id}",
            project_id=config.project_id,
            mode="code",
        )
    console.print(
        Panel.fit(
            f"[green]baird code[/green]  project={config.project_id}  model={config.model}\n"
            f"session={session['id'][:8]}  /help for commands, /exit to quit",
            border_style="green",
        )
    )

    iterator: Optional[Iterable[str]] = iter(inputs) if inputs is not None else None

    while True:
        try:
            if iterator is not None:
                try:
                    raw = next(iterator)
                except StopIteration:
                    break
            else:
                raw = input_fn("user> ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            break

        # Multi-line support: a single line of `"""` opens a heredoc-style
        # block; another `"""` closes it. Lines collapsed into one message.
        if raw.strip() == '"""':
            buf: list[str] = []
            while True:
                try:
                    nxt = next(iterator) if iterator is not None else input_fn("... ")
                except (StopIteration, EOFError, KeyboardInterrupt):
                    break
                if nxt.strip() == '"""':
                    break
                buf.append(nxt.rstrip("\n"))
            raw = "\n".join(buf)

        line = raw.strip()
        if not line:
            continue

        if line.startswith("/"):
            # First chance to handle: the hub-first slash registry in
            # baird/slash.py (project/host/env/where/run on …). Returns None
            # when the line doesn't match a registered verb, in which case
            # the legacy REPL-internal commands below take a turn.
            from .agent_tools import ToolEnv
            from .slash import SlashContext, try_dispatch as _try_slash

            slash_ctx = SlashContext(
                hub=hub,
                env=ToolEnv(hub=hub, project_id=config.project_id),
                input_fn=input_fn if iterator is None else _iter_input_fn(iterator),
                console=console,
                active_host=getattr(config, "_active_host", None),
            )
            slash_res = _try_slash(line[1:], slash_ctx)
            if slash_res is not None:
                if slash_res.output:
                    style = "green" if slash_res.ok else "red"
                    console.print(f"[{style}]{slash_res.output}[/{style}]")
                if slash_res.active_host:
                    config._active_host = slash_res.active_host  # type: ignore[attr-defined]
                if slash_res.switch_to_project:
                    swapped = _switch_project(
                        slash_res.switch_to_project, hub, config, host_id, console
                    )
                    if swapped[0] is not None:
                        rendered, system, repo_ctx, session = swapped
                continue

            cmd = line[1:].split()[0].lower()
            if cmd in {"exit", "quit"}:
                break
            if cmd == "context":
                console.print(rendered)
                continue
            if cmd == "reset":
                session = hub.new_session(
                    mode="code",
                    project_id=config.project_id,
                    task_id=f"repl-{config.project_id}",
                )
                console.print(f"[yellow]new session[/yellow] {session['id'][:8]}")
                continue
            if cmd == "cost":
                console.print(
                    f"[dim]turns={stats.turns}  cost=${stats.total_cost_usd:.4f}  "
                    f"tokens={stats.total_input_tokens}→{stats.total_output_tokens}[/dim]"
                )
                continue
            if cmd in {"no-diff", "nodiff"}:
                diff_loop_active = False
                console.print("[yellow]diff prompts disabled for this session[/yellow]")
                continue
            if cmd == "model":
                parts = line.split(maxsplit=1)
                arg = parts[1].strip() if len(parts) == 2 else ""
                if not arg:
                    console.print(f"[dim]current model: {config.model}[/dim]")
                    try:
                        from .model import top_openrouter_models

                        picks = top_openrouter_models(n=20)
                        model_picker_cache = [m.get("id", "") for m in picks]
                        for i, m in enumerate(picks, 1):
                            console.print(
                                f"  [cyan]{i:>2}.[/cyan] {m.get('id','')}"
                            )
                        console.print(
                            "[dim]usage: /model <number> or /model <full-id>[/dim]"
                        )
                    except Exception as e:
                        console.print(
                            f"[yellow]could not fetch model list ({e}); "
                            "you can still type /model <full-id>[/yellow]"
                        )
                else:
                    new_model: str | None = None
                    if arg.isdigit() and model_picker_cache:
                        idx = int(arg)
                        if 1 <= idx <= len(model_picker_cache):
                            new_model = model_picker_cache[idx - 1]
                        else:
                            console.print(
                                f"[red]index {idx} out of range[/red] "
                                f"(1..{len(model_picker_cache)})"
                            )
                    else:
                        new_model = arg
                    if new_model:
                        old = config.model
                        config.model = new_model
                        console.print(f"[yellow]model:[/yellow] {old} → {new_model}")
                continue
            if cmd == "project":
                from .context_loader import lite_repo_context
                from .project_yaml import ProjectYaml

                parts = line.split()
                if len(parts) == 1:
                    rows = hub.list_projects()
                    if not rows:
                        console.print("[dim]no projects on the hub[/dim]")
                    else:
                        for r in rows:
                            marker = " [green]*[/green]" if r["id"] == config.project_id else "  "
                            console.print(
                                f"{marker} [cyan]{r['id']}[/cyan]  {r.get('name','')}"
                            )
                        console.print(
                            "[dim]switch: /project <id>   create: /project new <id> [name][/dim]"
                        )
                    continue
                sub = parts[1]
                if sub == "new":
                    if len(parts) < 3:
                        console.print("[red]usage:[/red] /project new <id> [name]")
                        continue
                    new_id = parts[2]
                    new_name = " ".join(parts[3:]) if len(parts) > 3 else new_id
                    try:
                        hub.upsert_project(id=new_id, name=new_name)
                    except Exception as e:
                        console.print(f"[red]create failed:[/red] {e}")
                        continue
                    console.print(f"[green]created project {new_id}[/green]")
                    target_id = new_id
                else:
                    target_id = sub
                try:
                    proj_row = hub.get_project(target_id)
                except Exception as e:
                    console.print(f"[red]project '{target_id}' not on hub:[/red] {e}")
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
                console.print(
                    f"[yellow]switched to project[/yellow] {target_id}  "
                    f"session={session['id'][:8]}"
                )
                continue
            if cmd == "sessions":
                rows = hub.list_sessions(project_id=config.project_id, limit=20)
                if not rows:
                    console.print("[dim]no prior sessions for this project[/dim]")
                else:
                    console.print(f"[dim]sessions for {config.project_id}[/dim]")
                    for r in rows:
                        marker = " [green]*[/green]" if r["id"] == session["id"] else "  "
                        console.print(
                            f"{marker} {r['id'][:8]}  {r.get('mode','?')}  "
                            f"started={r.get('started_at','')[:19]}"
                        )
                    console.print(
                        "[dim]resume one with: baird code --session <full-id>[/dim]"
                    )
                continue
            if cmd == "help":
                from .slash import commands as _slash_cmds

                console.print(
                    "[dim]/exit  /context  /reset  /cost  /model [id]  "
                    "/sessions  /project [id|new <id>]  /no-diff[/dim]"
                )
                console.print(
                    "[dim]hub-first: "
                    + "  ".join(f"/{c}" for c in _slash_cmds())
                    + "[/dim]"
                )
                continue
            console.print(f"[red]unknown command:[/red] /{cmd} (try /help)")
            continue

        try:
            completion = _one_turn(
                user_msg=line,
                hub=hub,
                model_client=model_client,
                session_id=session["id"],
                config=config,
                system=system,
                host_id=host_id,
            )
        except ModelError as e:
            console.print(f"[red]model error:[/red] {e}")
            continue

        console.print(completion.content)
        console.print(
            f"[dim]model={completion.model}  "
            f"tokens={completion.usage.input_tokens}→{completion.usage.output_tokens}  "
            f"cost=${completion.cost_usd:.4f}[/dim]"
        )
        stats.turns += 1
        stats.total_cost_usd += completion.cost_usd
        stats.total_input_tokens += completion.usage.input_tokens
        stats.total_output_tokens += completion.usage.output_tokens

        if diff_loop_active and config.project_root is not None:
            _handle_diff_blocks(
                completion.content,
                console=console,
                project_root=config.project_root,
                input_fn=input_fn if iterator is None else _iter_input_fn(iterator),
            )

    console.print(
        f"[dim]session={session['id'][:8]}  turns={stats.turns}  total=${stats.total_cost_usd:.4f}[/dim]"
    )
    return stats


def _one_turn(
    *,
    user_msg: str,
    hub: HubClient,
    model_client: OpenRouterClient,
    session_id: str,
    config: ReplConfig,
    system: str,
    host_id: str | None,
    on_chunk: Callable[[str], None] | None = None,
) -> Completion:
    """Append the user message, call the model with recent history, record an
    Action with cost, append the assistant message. Returns the Completion.

    When `on_chunk` is set, calls `stream_complete` and reports partial
    content via the callback as it arrives.
    """
    with hub.start_action(
        project_id=config.project_id,
        tool_name="model",
        command="repl",
        host=host_id,
        model_name=config.model,
    ) as action:
        from .context_compressor import load_history_with_summary

        msgs = load_history_with_summary(
            hub,
            session_id=session_id,
            cap=config.history_cap,
            model_client=model_client,
            summary_model=config.model,
        )
        hub.append_message(session_id, role="user", content=user_msg)
        msgs.append({"role": "user", "content": user_msg})

        if on_chunk is not None:
            completion = model_client.stream_complete(
                model=config.model,
                messages=msgs,
                max_tokens=config.max_tokens,
                temperature=config.temperature,
                system=system,
                on_chunk=on_chunk,
            )
        else:
            completion = model_client.complete(
                model=config.model,
                messages=msgs,
                max_tokens=config.max_tokens,
                temperature=config.temperature,
                system=system,
            )

        hub.append_message(session_id, role="assistant", content=completion.content)
        action.record_usage(
            cost_usd=completion.cost_usd,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
        )
        # Summary for the action row — first line of the reply (or truncated).
        first_line = completion.content.strip().splitlines()[0] if completion.content else ""
        action.set_summary(first_line[:200])

    return completion


def _switch_project(target_id, hub, config, host_id, console):
    """Reload context/session for `target_id`. Used by /project switch and by
    slash commands that signal `switch_to_project` (e.g. /project new)."""
    from .context_loader import lite_repo_context
    from .project_yaml import ProjectYaml

    try:
        proj_row = hub.get_project(target_id)
    except Exception as e:
        console.print(f"[red]project '{target_id}' not on hub:[/red] {e}")
        return None, None, None, None
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
    console.print(
        f"[yellow]switched to project[/yellow] {target_id}  "
        f"session={session['id'][:8]}"
    )
    return rendered, system, repo_ctx, session


def _iter_input_fn(iterator: Iterable[str]) -> Callable[[str], str]:
    """Adapt an `inputs=` iterable so the diff prompt can read the next line.

    Returns "" on exhaustion (treated as "no apply") — the prompt skips.
    """
    def _read(_: str) -> str:
        try:
            return next(iterator)  # type: ignore[arg-type]
        except StopIteration:
            return ""

    return _read


def _handle_diff_blocks(
    content: str,
    *,
    console: Console,
    project_root: Path,
    input_fn: Callable[[str], str],
) -> None:
    """Find fenced diff blocks in `content` and prompt the user per block."""
    blocks = extract_diff_blocks(content)
    if not blocks:
        return
    for i, diff in enumerate(blocks, 1):
        console.print(
            Panel(
                Syntax(diff, "diff", line_numbers=False, word_wrap=True),
                title=f"proposed diff {i}/{len(blocks)}",
                border_style="cyan",
            )
        )
        try:
            choice = input_fn("apply? [y/N/q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if choice == "q":
            return
        if choice != "y":
            console.print("[dim]skipped[/dim]")
            continue
        try:
            result = apply_diff_to_repo(
                repo=project_root,
                diff_text=diff,
                commit_message=f"baird: apply REPL-proposed diff {i}",
                action_id=f"repl-{uuid.uuid4().hex[:8]}",
            )
        except DiffApplyError as e:
            console.print(f"[red]apply failed:[/red] {e}")
            continue
        console.print(
            f"[green]applied[/green] {result.commit_sha[:12]} "
            f"({len(result.files_changed)} file(s))"
        )
