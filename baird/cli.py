"""BAIRD CLI entry — `baird` command.

Minimum-viable surface from the Phase 5 design. Phase 2 wires up:
- `baird project init / push / pull / list`
- `baird inbox list / resolve`

Coding mode, chat, and task execution still wait on later phases.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import load_host_config, load_hub_config
from .memory_client import HubClient
from .project_yaml import (
    ProjectYaml,
    load_project_yaml,
    project_yaml_template,
    save_project_yaml,
)

app = typer.Typer(
    no_args_is_help=False,  # we render mode hints + help from the callback ourselves
    add_completion=False,
    invoke_without_command=True,
    help="BAIRD — Bioinformatics AI Research Daemon",
)
project_app = typer.Typer(help="Project management")
task_app = typer.Typer(help="Background task management")
hub_app = typer.Typer(help="Hub service")
inbox_app = typer.Typer(help="Notification inbox", invoke_without_command=True)
diff_app = typer.Typer(help="Diff review/apply")
orchestrator_app = typer.Typer(help="Background-agent scheduler")
registry_app = typer.Typer(help="Registry queries")
session_app = typer.Typer(help="Multiplexer (tmux/screen) sessions")
satellite_app = typer.Typer(help="Enrol + manage satellite machines")

app.add_typer(project_app, name="project")
app.add_typer(task_app, name="task")
app.add_typer(hub_app, name="hub")
app.add_typer(inbox_app, name="inbox")
app.add_typer(diff_app, name="diff")
app.add_typer(orchestrator_app, name="orchestrator")
app.add_typer(registry_app, name="registry")
app.add_typer(session_app, name="session")
app.add_typer(satellite_app, name="satellite")

console = Console()


# ----- shared helpers -----


def _hub_client_from_host(auto_start: bool = True) -> HubClient:
    """Build a HubClient from host.yaml (the satellite-side config).

    Falls back to config.yaml's `listen` if host.yaml is missing, so this also
    works on the hub itself for local CLI use. Both files live under
    `$BAIRD_HOME` (defaulting to `~/.baird`).

    If `auto_start` (default), spawns the hub in the background when it isn't
    already running. Pass `auto_start=False` for commands like `baird hub serve`
    that *are* the hub.
    """
    from . import paths as _paths

    if auto_start:
        from .supervisor import ensure_hub_running

        ensure_hub_running()

    host_path = _paths.host_yaml_path()
    if host_path.exists():
        host_cfg = load_host_config(host_path)
        return HubClient(host_cfg.hub_url, host_cfg.effective_hub_token())
    hub_cfg = load_hub_config()
    host, port = hub_cfg.listen.split(":")
    return HubClient(f"http://{host}:{port}", hub_cfg.auth_token)


def _project_yaml_path(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / ".baird" / "project.yaml"


def _run_ink_tui(config) -> None:  # noqa: ANN001
    """Spawn the Ink TUI frontend as a child process."""
    ink_dir = Path(__file__).resolve().parent.parent / "baird-ink"
    if not (ink_dir / "src" / "cli.tsx").exists():
        console.print(f"[red]baird-ink frontend not found at {ink_dir}[/red]")
        raise typer.Exit(1)

    # Auto-install node dependencies if missing (gitignored)
    if not (ink_dir / "node_modules" / "ink" / "package.json").exists():
        console.print("[cyan]installing baird-ink dependencies… (one-time)[/cyan]")
        try:
            subprocess.run(["npm", "install"], cwd=str(ink_dir), check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            console.print(f"[red]npm install failed: {e}[/red]")
            console.print("[yellow]run 'cd baird-ink && npm install' manually[/yellow]")
            raise typer.Exit(1)

    # Resolve the correct Python command (venv-aware)
    python_cmd = os.environ.get("BAIRD_PYTHON_CMD") or sys.executable
    if "uv" not in python_cmd:
        try:
            result = subprocess.run(
                ["uv", "run", "which", "python"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                python_cmd = result.stdout.strip()
        except Exception:
            pass  # keep default

    env = {
        **os.environ,
        "BAIRD_PYTHON_CMD": python_cmd,
    }
    result = subprocess.run(
        ["npx", "--yes", "tsx", "src/cli.tsx"],
        cwd=str(ink_dir),
        env=env,
    )
    if result.returncode != 0:
        console.print(f"[dim]ink TUI exited ({result.returncode})[/dim]")


# ----- top-level callback -----


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show version and exit"),
) -> None:
    if version:
        console.print(f"baird {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        _print_mode_hint()
        console.print()
        console.print(ctx.get_help())
        raise typer.Exit()


def _print_mode_hint() -> None:
    """Phase 5 #1/#2: mode auto-detection (hint-only for now)."""
    cwd = Path.cwd()
    project_yaml = cwd / ".baird" / "project.yaml"
    if project_yaml.exists():
        try:
            from .project_yaml import load_project_yaml
            py = load_project_yaml(project_yaml)
            console.print(f"[green]Detected project:[/green] {py.name} ({py.id}) — try [bold]`baird code`[/bold]")
        except Exception:
            console.print("[yellow]Detected .baird/project.yaml but failed to parse — try `baird project init --force` to reset[/yellow]")
        return
    if (cwd / ".git").exists():
        console.print("[cyan]Plain git repo — run [bold]`baird project init <id>`[/bold] to enrol it[/cyan]")
        return
    notes_markers = {"papers", "notes", "research"}
    if cwd.name in notes_markers or any((cwd / m).exists() for m in notes_markers):
        console.print("[cyan]Looks like a notes/research dir — try [bold]`baird chat`[/bold] (no project context)[/cyan]")
        return
    console.print("[dim]No project here. Use [bold]`baird chat`[/bold] for free-form, or `baird project init` to start one.[/dim]")


@app.command()
def code(
    show_context: bool = typer.Option(
        False, "--show-context", help="Print the rendered repo context and exit"
    ),
    file: list[str] = typer.Option(
        [], "--file", "-f", help="Extra files to include in the context block"
    ),
    budget: int = typer.Option(6000, "--budget", help="Approx token budget for the context block"),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="OpenRouter model id. Falls back to $OPENROUTER_MODEL, then the REPL default.",
    ),
    session: str | None = typer.Option(
        None, "--session", help="Resume a specific session id instead of the project default."
    ),
    project: str | None = typer.Option(
        None, "--project", "-p",
        help="Run against a project from the hub (no .baird/project.yaml needed).",
    ),
    tui: bool = typer.Option(
        True, "--tui/--no-tui",
        help="Default is the persistent layout; --no-tui falls back to the line REPL.",
    ),
    mode: str = typer.Option(
        "build", "--mode", "-M",
        help="Agent mode: 'build' (full access, default) or 'plan' (read-only analysis). Tab switches in TUI.",
    ),
) -> None:
    """Interactive coding mode.

    Three ways to pick a project:
      1. `--project <id>` — pull it from the hub, no local checkout needed
      2. `.baird/project.yaml` in cwd — current behaviour, full repo context
      3. neither — falls into the scratch project (chat-style, no diff loop)
    """
    from .context_loader import load_repo_context, lite_repo_context, render_context
    from .project_yaml import ProjectYaml

    root = Path.cwd()
    has_local = (root / ".baird" / "project.yaml").exists()

    if project is not None:
        with _hub_client_from_host() as hub:
            if project == "scratch":
                hub.upsert_project(id="scratch", name="Scratch", context="Ad-hoc work.")
            try:
                proj_row = hub.get_project(project)
            except Exception as e:
                console.print(f"[red]could not load project '{project}' from hub:[/red] {e}")
                raise typer.Exit(1)
            py = ProjectYaml(
                id=proj_row["id"],
                name=proj_row.get("name") or proj_row["id"],
                github=proj_row.get("github"),
                context=proj_row.get("context"),
                parent_id=proj_row.get("parent_id")
                or (proj_row.get("config") or {}).get("parent_id"),
            )
            ctx = lite_repo_context(py, hub=hub)
    elif has_local:
        try:
            with _hub_client_from_host() as hub:
                ctx = load_repo_context(root, hub=hub)
        except Exception:
            ctx = load_repo_context(root, hub=None)
    else:
        with _hub_client_from_host() as hub:
            hub.upsert_project(id="scratch", name="Scratch", context="Ad-hoc work, no project committed yet.")
            proj_row = hub.get_project("scratch")
            py = ProjectYaml(id="scratch", name="Scratch", context=proj_row.get("context"))
            ctx = lite_repo_context(py, hub=hub)
        console.print("[dim]no project here — using scratch project. /project to switch or create one.[/dim]")

    if show_context:
        console.print(render_context(ctx, token_budget=budget))
        return

    # Multi-turn REPL (Phase 4b). Diff loop + tool calls come in a later slice.
    from .model import OpenRouterClient, make_hub_proxy_transport
    from .repl import ReplConfig, run_repl

    import os as _os
    resolved_model = model or _os.environ.get("OPENROUTER_MODEL")
    repl_cfg_kwargs: dict[str, object] = {
        "project_id": ctx.project.id,
        "project_root": ctx.project_root,
        "agent_mode": mode,
    }
    if resolved_model:
        repl_cfg_kwargs["model"] = resolved_model

    # Pick the model transport. If host.yaml says `use_hub_for_models: true`,
    # route via the hub proxy (key lives only on the hub); otherwise direct.
    from . import paths as _paths

    transport = None
    host_path = _paths.host_yaml_path()
    if host_path.exists():
        host_cfg = load_host_config(host_path)
        if host_cfg.use_hub_for_models:
            transport = make_hub_proxy_transport(
                hub_url=host_cfg.hub_url,
                auth_token=host_cfg.effective_hub_token(),
            )

    if tui:
        _run_ink_tui(config=ReplConfig(**repl_cfg_kwargs))
        return

    with _hub_client_from_host() as hub:
        run_repl(
            repo_ctx=ctx,
            hub=hub,
            model_client=OpenRouterClient(transport=transport),
            config=ReplConfig(**repl_cfg_kwargs),
            console=console,
            session_id=session,
        )


@app.command()
def chat(
    model: str | None = typer.Option(None, "--model", "-m"),
    session: str | None = typer.Option(None, "--session"),
    tui: bool = typer.Option(True, "--tui/--no-tui"),
) -> None:
    """Free-form chat — runs `baird code` against the scratch project.

    Use `/project <id>` mid-session to switch into a real project, or
    `/project new <id>` to create one from inside the chat.
    """
    # Delegate by invoking code() with project=None and no .baird/project.yaml
    # in cwd → the scratch fall-through kicks in. We can't easily call the
    # Typer command directly with options, so inline the same logic with
    # project="scratch" forced.
    code(  # type: ignore[arg-type]
        show_context=False,
        file=[],
        budget=6000,
        model=model,
        session=session,
        project="scratch",
        tui=tui,
    )


# ----- status / logs / ps / registry -----


@app.command()
def status(
    watch: bool = typer.Option(False, "--watch", help="Live-refresh the dashboard"),
    interval: float = typer.Option(2.0, "--interval", help="Refresh seconds when --watch"),
) -> None:
    """One-shot dashboard (or live with --watch)."""
    import time as _time

    from rich.console import Group
    from rich.live import Live

    from .config import load_hub_config
    from .dashboard import (
        _budget_panel,
        _counts_panel,
        _inbox_panel,
        _recent_actions_panel,
        _tasks_panel,
        render,
    )
    from .tasks import load_tasks_dir

    hub_cfg = load_hub_config()
    tasks_dir = _tasks_dir()

    if not watch:
        with _hub_client_from_host() as hub:
            state = dashboard_gather(hub, hub_cfg, load_tasks_dir(tasks_dir))
        render(state, console)
        return

    with _hub_client_from_host() as hub:
        def _snapshot() -> Group:
            tasks = load_tasks_dir(tasks_dir)
            state = dashboard_gather(hub, hub_cfg, tasks)
            if not state.hub_ok:
                from rich.panel import Panel as _Panel
                return Group(_Panel.fit(
                    f"[red]hub unreachable[/red] {state.hub_url}\n{state.error or ''}",
                    title="status", border_style="red",
                ))
            panels = [
                _counts_panel(state),
                _budget_panel(state),
                _inbox_panel(state),
                _recent_actions_panel(state),
            ]
            if state.tasks:
                panels.append(_tasks_panel(state))
            return Group(*panels)

        with Live(_snapshot(), console=console, refresh_per_second=4, screen=False) as live:
            try:
                while True:
                    _time.sleep(interval)
                    live.update(_snapshot())
            except KeyboardInterrupt:
                pass


# Indirection so tests can monkeypatch the gather call point.
def dashboard_gather(hub, hub_cfg, tasks):  # noqa: ANN001
    from .dashboard import gather
    return gather(hub=hub, hub_cfg=hub_cfg, tasks=tasks)


@app.command()
def logs(
    action_id: str = typer.Argument(...),
    n_messages: int = typer.Option(50, "--messages", help="How many session messages to include"),
) -> None:
    """Show one Action plus any conversation messages from its task session."""
    from rich.panel import Panel

    with _hub_client_from_host() as hub:
        try:
            action = hub.get_action(action_id)
        except Exception as e:
            console.print(f"[red]action not found:[/red] {e}")
            raise typer.Exit(1) from e

        # Find a session keyed to the same task (best-effort — sessions/{id} index by task isn't a route yet).
        msgs: list[dict] = []
        # We can't query sessions by task_id without a dedicated route; punt for now.
        # The action summary plus its metadata is the load-bearing output.

    body = (
        f"id:           {action['id']}\n"
        f"project:      {action.get('project_id') or '-'}\n"
        f"task:         {action.get('task_id') or '-'}\n"
        f"host:         {action.get('host') or '-'}\n"
        f"tool/cmd:     {action.get('command') or action.get('tool_name') or '?'}\n"
        f"model:        {action.get('model_name') or '-'}\n"
        f"started_at:   {action.get('started_at')}\n"
        f"finished_at:  {action.get('finished_at') or '(running)'}\n"
        f"exit_code:    {action.get('exit_code')}\n"
        f"cost_usd:     ${action.get('cost_usd') or 0:.4f}\n"
        f"tokens:       in={action.get('input_tokens') or 0} out={action.get('output_tokens') or 0}\n"
    )
    console.print(Panel(body, title=f"action {action['id'][:8]}", border_style="cyan"))
    if action.get("summary"):
        console.print(Panel(action["summary"], title="summary", border_style="green"))


@app.command()
def ps(limit: int = typer.Option(50, "--limit")) -> None:
    """Actions currently running (no finished_at)."""
    with _hub_client_from_host() as hub:
        rows = hub.list_actions(unfinished_only=True, limit=limit)
    if not rows:
        console.print("[dim]nothing running[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("started", style="dim")
    table.add_column("id", style="dim")
    table.add_column("project")
    table.add_column("task")
    table.add_column("host")
    table.add_column("tool/cmd")
    for a in rows:
        table.add_row(
            (a.get("started_at") or "")[:19],
            a["id"][:8],
            a.get("project_id") or "-",
            a.get("task_id") or "-",
            a.get("host") or "-",
            (a.get("command") or a.get("tool_name") or "?")[:50],
        )
    console.print(table)


# ----- registry -----


@registry_app.command("actions")
def registry_actions(
    project_id: str | None = typer.Option(None, "--project"),
    task_id: str | None = typer.Option(None, "--task"),
    since_hours: int | None = typer.Option(None, "--since-hours", help="e.g. 24, 168"),
    unfinished: bool = typer.Option(False, "--unfinished"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List actions with filters."""
    import datetime as _dt
    started_after = None
    if since_hours is not None:
        started_after = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=since_hours)
    with _hub_client_from_host() as hub:
        rows = hub.list_actions(
            project_id=project_id,
            task_id=task_id,
            started_after=started_after,
            unfinished_only=unfinished,
            limit=limit,
        )
    if not rows:
        console.print("[dim]no actions[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("started", style="dim")
    table.add_column("id", style="dim")
    table.add_column("project")
    table.add_column("task")
    table.add_column("status")
    table.add_column("cost")
    table.add_column("summary", overflow="ellipsis")
    for a in rows:
        status_ = "…" if a.get("finished_at") is None else f"exit {a.get('exit_code')}"
        table.add_row(
            (a.get("started_at") or "")[:19],
            a["id"][:8],
            a.get("project_id") or "-",
            a.get("task_id") or "-",
            status_,
            f"${a.get('cost_usd') or 0:.4f}",
            (a.get("summary") or "")[:80],
        )
    console.print(table)


# ----- inbox -----


@inbox_app.callback()
def inbox_default(
    ctx: typer.Context,
    unresolved: bool = typer.Option(False, "--unresolved", help="Show only unresolved items"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Show notifications (default = list)."""
    if ctx.invoked_subcommand is not None:
        return
    with _hub_client_from_host() as hub:
        rows = hub.list_notifications(unresolved_only=unresolved, limit=limit)
    if not rows:
        console.print("[dim]inbox empty[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("id", overflow="fold")
    table.add_column("kind")
    table.add_column("title")
    table.add_column("created")
    table.add_column("resolved")
    for r in rows:
        table.add_row(
            r["id"][:8],
            r["kind"],
            r["title"],
            r["created_at"][:19],
            r.get("resolution") or "",
        )
    console.print(table)


@inbox_app.command("resolve")
def inbox_resolve(notif_id: str, resolution: str = typer.Argument("accept")) -> None:
    """Mark a notification resolved (`accept` / `reject` / free-form)."""
    with _hub_client_from_host() as hub:
        row = hub.resolve_notification(notif_id, resolution=resolution)
    console.print(f"[green]resolved[/green] {row['id']} → {row['resolution']}")


# ----- project -----


@project_app.command("init")
def project_init(
    project_id: str = typer.Argument(..., help="Stable project id (slug)"),
    name: str = typer.Option(None, "--name", help="Human-readable name (defaults to id)"),
    github: str | None = typer.Option(None, "--github"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing .baird/project.yaml"),
) -> None:
    """Create `.baird/project.yaml` in the current directory."""
    path = _project_yaml_path()
    if path.exists() and not force:
        console.print(f"[red]{path} already exists (use --force to overwrite)[/red]")
        raise typer.Exit(1)
    template = project_yaml_template(project_id, name or project_id, github=github)
    save_project_yaml(template, path)
    console.print(f"[green]wrote[/green] {path}")


@project_app.command("push")
def project_push() -> None:
    """Upsert the project (from `.baird/project.yaml` in cwd) into the hub."""
    path = _project_yaml_path()
    if not path.exists():
        console.print(f"[red]no project.yaml at {path}[/red] — run `baird project init`")
        raise typer.Exit(1)
    py: ProjectYaml = load_project_yaml(path)
    with _hub_client_from_host() as hub:
        result = hub.upsert_project(
            id=py.id,
            name=py.name,
            github=py.github,
            context=py.context,
            config=py.model_dump(mode="json", exclude={"id", "name", "github", "context"}),
        )
    console.print(f"[green]pushed[/green] {result['id']} → hub")


@project_app.command("pull")
def project_pull(
    project_id: str = typer.Argument(...),
    out: Path = typer.Option(None, "--out", help="Destination directory (defaults to cwd)"),
) -> None:
    """Pull a project's record from the hub and write `.baird/project.yaml`."""
    with _hub_client_from_host() as hub:
        row = hub.get_project(project_id)
    cfg = row.get("config") or {}
    py = ProjectYaml(
        id=row["id"],
        name=row["name"],
        github=row.get("github"),
        context=row.get("context"),
        **cfg,
    )
    dest = _project_yaml_path(out)
    save_project_yaml(py, dest)
    console.print(f"[green]wrote[/green] {dest}")


@project_app.command("list")
def project_list() -> None:
    """List projects known to the hub."""
    with _hub_client_from_host() as hub:
        rows = hub.list_projects()
    if not rows:
        console.print("[dim]no projects[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("id")
    table.add_column("name")
    table.add_column("github")
    table.add_column("created")
    for r in rows:
        table.add_row(r["id"], r["name"], r.get("github") or "", r["created_at"][:10])
    console.print(table)


# ----- task -----


def _tasks_dir() -> Path:
    from . import paths as _paths

    return _paths.tasks_dir()


@task_app.command("add")
def task_add(task_id: str, force: bool = typer.Option(False, "--force")) -> None:
    """Write a starter `~/.baird/tasks/<id>.yaml`."""
    from .tasks import save_task, task_yaml_template

    path = _tasks_dir() / f"{task_id}.yaml"
    if path.exists() and not force:
        console.print(f"[red]{path} already exists (use --force)[/red]")
        raise typer.Exit(1)
    save_task(task_yaml_template(task_id), path)
    console.print(f"[green]wrote[/green] {path}")


@task_app.command("list")
def task_list() -> None:
    """List tasks known to the orchestrator."""
    from .tasks import load_tasks_dir

    tasks = load_tasks_dir(_tasks_dir())
    if not tasks:
        console.print("[dim]no tasks[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("id")
    table.add_column("trigger")
    table.add_column("enabled")
    table.add_column("model")
    table.add_column("budget")
    for t in tasks.values():
        trig = t.trigger.type
        if hasattr(t.trigger, "cron"):
            trig = f"cron({t.trigger.cron})"  # type: ignore[attr-defined]
        elif hasattr(t.trigger, "interval_seconds"):
            trig = f"every({t.trigger.interval_seconds}s)"  # type: ignore[attr-defined]
        table.add_row(
            t.id,
            trig,
            "yes" if t.enabled else "no",
            t.runnable.model,
            f"${t.budget.max_cost_usd:.2f}" if t.budget.max_cost_usd else "-",
        )
    console.print(table)


@task_app.command("history")
def task_history(
    task_id: str,
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Show recent firings of one task."""
    with _hub_client_from_host() as hub:
        rows = hub.list_actions(task_id=task_id, limit=limit)
    if not rows:
        console.print(f"[dim]no firings recorded for {task_id}[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("started", style="dim")
    table.add_column("id", style="dim")
    table.add_column("exit")
    table.add_column("cost")
    table.add_column("summary", overflow="ellipsis")
    for a in rows:
        table.add_row(
            (a.get("started_at") or "")[:19],
            a["id"][:8],
            str(a.get("exit_code") if a.get("exit_code") is not None else "…"),
            f"${a.get('cost_usd') or 0:.4f}",
            (a.get("summary") or "")[:80],
        )
    console.print(table)


@task_app.command("run")
def task_run(task_id: str) -> None:
    """Fire one task once, ignoring the schedule."""
    from .model import OpenRouterClient
    from .runner import run_task_once
    from .tasks import load_tasks_dir

    tasks = load_tasks_dir(_tasks_dir())
    task = tasks.get(task_id)
    if task is None:
        console.print(f"[red]no task {task_id} in {_tasks_dir()}[/red]")
        raise typer.Exit(1)

    with _hub_client_from_host() as hub:
        client = OpenRouterClient()
        result = run_task_once(task, hub=hub, model_client=client)
    console.print(f"[green]fired[/green] action={result.action_id[:12]} cost=${result.completion.cost_usd:.4f}")
    console.print(result.summary or "")


# ----- hub -----


@hub_app.command("serve")
def hub_serve(
    host: str | None = typer.Option(None, "--host", help="Override host from config.yaml"),
    port: int | None = typer.Option(None, "--port", help="Override port from config.yaml"),
) -> None:
    """Run the BAIRD hub FastAPI service.

    Defaults to the `listen:` value in `<baird_home>/config.yaml`
    (`127.0.0.1:8000` if no config file exists).
    """
    import uvicorn

    cfg = load_hub_config()
    default_host, default_port = cfg.listen.split(":")
    bind_host = host or default_host
    bind_port = port if port is not None else int(default_port)
    console.print(f"[green]starting BAIRD hub on {bind_host}:{bind_port}[/green]")
    uvicorn.run("baird.hub:app", host=bind_host, port=bind_port, log_level="info")


@hub_app.command("install")
def hub_install(
    system: bool = typer.Option(
        False,
        "--system",
        help="Install system-wide (writes /etc/systemd/system, needs sudo). "
        "Default is --user (no sudo; needs `loginctl enable-linger` to survive logout).",
    ),
) -> None:
    """Install systemd units so the hub + local daemon survive reboots.

    Mirrors what `baird up` does for the running shell, but persistent.
    Writes two units (`baird-hub.service`, `baird-daemon.service`), reloads
    systemd, and enables + starts both.
    """
    from .hub_install import InstallSpec, install

    spec = InstallSpec(scope="system" if system else "user")
    units = install(spec)
    scope_label = "system-wide" if system else "user-scope"
    console.print(f"[green]installed {scope_label}: {', '.join(units)}[/green]")
    if not system:
        console.print(
            "[yellow]tip:[/yellow] services stop at logout unless you run "
            "`sudo loginctl enable-linger $USER` once (one-time)."
        )


@hub_app.command("uninstall")
def hub_uninstall(
    system: bool = typer.Option(False, "--system", help="Remove the system-wide install"),
) -> None:
    """Disable + remove the installed systemd units (idempotent)."""
    from .hub_install import InstallSpec, uninstall

    spec = InstallSpec(scope="system" if system else "user")
    uninstall(spec)
    console.print("[green]uninstalled[/green]")


# ----- top-level supervisor commands -----


@app.command("up")
def up(
    hub_only: bool = typer.Option(
        False, "--hub-only", help="Start only the hub; skip the local daemon."
    ),
) -> None:
    """Start the hub (and the local daemon) in the background if not already up."""
    from .supervisor import (
        ensure_daemon_running,
        ensure_hub_running,
        is_daemon_running,
        is_hub_running,
    )

    if is_hub_running():
        console.print("[green]hub is already running[/green]")
    else:
        ensure_hub_running(quiet=True)
        console.print("[green]hub started[/green]")
    if hub_only:
        return
    if is_daemon_running():
        console.print("[green]daemon is already running[/green]")
    else:
        ensure_daemon_running(quiet=True)
        console.print("[green]daemon started[/green]")


@app.command("stop")
def stop(
    hub_only: bool = typer.Option(
        False, "--hub-only", help="Stop only the hub; leave the daemon running."
    ),
) -> None:
    """Stop the background hub and daemon started by `baird up`."""
    from .supervisor import stop_daemon, stop_hub

    stopped: list[str] = []
    if not hub_only and stop_daemon():
        stopped.append("daemon")
    if stop_hub():
        stopped.append("hub")
    if stopped:
        console.print(f"[green]stopped: {', '.join(stopped)}[/green]")
    else:
        console.print("[yellow]nothing supervised to stop[/yellow]")


@app.command("restart")
def restart(
    hub_only: bool = typer.Option(
        False, "--hub-only", help="Restart only the hub; leave the daemon untouched."
    ),
) -> None:
    """Stop and re-start the hub (and the local daemon). One command, picks up
    any code changes the running processes were still holding."""
    import time as _time

    from .supervisor import (
        ensure_daemon_running,
        ensure_hub_running,
        is_hub_running,
        stop_daemon,
        stop_hub,
    )

    if not hub_only:
        stop_daemon()
    stop_hub()
    # Brief settle so the new hub doesn't race the old one on the port.
    for _ in range(20):
        if not is_hub_running():
            break
        _time.sleep(0.1)
    ensure_hub_running(quiet=True)
    msg = ["hub"]
    if not hub_only:
        ensure_daemon_running(quiet=True)
        msg.append("daemon")
    console.print(f"[green]restarted: {', '.join(msg)}[/green]")


@app.command("update")
def update(
    sat_only: bool = typer.Option(
        False,
        "--sat-only",
        help="Only update satellites; skip local git pull and restart.",
    ),
    skip_satellites: bool = typer.Option(
        False,
        "--skip-satellites",
        help="Only update the local hub; skip satellite updates.",
    ),
) -> None:
    """Pull latest code from GitHub on the hub and all enrolled satellites,
    then restart services to pick up changes."""
    import subprocess as _subprocess
    import time as _time
    from pathlib import Path as _Path

    from .satellite import load_registry
    from .supervisor import (
        ensure_daemon_running,
        ensure_hub_running,
        is_hub_running,
        stop_daemon,
        stop_hub,
    )

    baird_dir = _Path(__file__).resolve().parent.parent

    # 1. Pull latest on the hub
    if not sat_only:
        console.print("[cyan]updating hub code…[/cyan]")
        r = _subprocess.run(
            ["git", "pull"],
            cwd=str(baird_dir),
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            console.print(f"[red]git pull failed on hub[/red]\n{r.stderr}")
            raise typer.Exit(1)
        console.print("[green]hub code updated[/green]")

    # 2. Update each satellite
    if not skip_satellites:
        reg = load_registry()
        if not reg:
            console.print("[yellow]no satellites enrolled[/yellow]")
        else:
            for host_id, entry in reg.items():
                ssh_host = entry.get("ssh_host", host_id)
                remote_dir = entry.get("remote_baird_dir", "$HOME/code/BAIRD")
                console.print(f"[cyan]updating {host_id} ({ssh_host})…[/cyan]")
                pull_script = (
                    f"cd {remote_dir} && "
                    "git fetch origin main 2>&1 && "
                    "git reset --hard origin/main 2>&1"
                )
                r = _subprocess.run(
                    ["ssh", ssh_host, pull_script],
                    capture_output=True, text=True, timeout=60,
                )
                if r.returncode != 0:
                    console.print(f"[red]{host_id} git pull failed[/red]\n{r.stdout[:500]}")
                else:
                    lines = [l for l in r.stdout.splitlines() if l.strip()]
                    for line in lines:
                        console.print(f"  {line}")
                console.print(f"[cyan]restarting daemon on {host_id}…[/cyan]")
                uv_bin = "\"$HOME/.local/bin/uv\""
                restart_script = (
                    "cd /tmp && "
                    "nohup env PATH=\"$HOME/.local/bin:$PATH\" "
                    "\"$HOME/.local/bin/uv\" run python -m baird.daemon "
                    "</dev/null >/tmp/baird-daemon.log 2>&1 & "
                    "disown; echo restart_ok"
                )
                try:
                    r2 = _subprocess.run(
                        ["ssh", ssh_host, restart_script],
                        capture_output=True, text=True, timeout=15,
                    )
                    if r2.returncode != 0:
                        console.print(f"[yellow]{host_id} restart failed[/yellow]\n{r2.stderr[:200] or r2.stdout[:200]}")
                    else:
                        console.print(f"[green]{host_id} updated[/green]")
                except Exception:
                    console.print(f"[yellow]{host_id} restart skipped (ssh timed out)[/yellow]")

    # 3. Restart local hub (+ daemon) with new code
    if not sat_only:
        console.print("[cyan]restarting local services…[/cyan]")
        stop_daemon()
        stop_hub()
        for _ in range(20):
            if not is_hub_running():
                break
            _time.sleep(0.1)
        ensure_hub_running(quiet=True)
        ensure_daemon_running(quiet=True)
        console.print("[green]hub + daemon restarted[/green]")


# ----- diff -----


@diff_app.command("apply")
def diff_apply_cmd(
    patch_file: Path = typer.Argument(..., exists=True, readable=True),
    message: str = typer.Option(..., "--message", "-m"),
    repo: Path = typer.Option(Path.cwd(), "--repo"),
    action_id: str | None = typer.Option(None, "--action-id"),
) -> None:
    """Apply a unified diff file to the repo as a single BAIRD commit."""
    import uuid

    from .diff_apply import DiffApplyError, apply_diff_to_repo

    try:
        result = apply_diff_to_repo(
            repo=repo,
            diff_text=patch_file.read_text(),
            commit_message=message,
            action_id=action_id or f"cli-{uuid.uuid4().hex[:8]}",
        )
    except DiffApplyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    console.print(f"[green]applied[/green] {result.commit_sha[:12]} ({len(result.files_changed)} files)")


@app.command()
def undo(repo: Path = typer.Option(Path.cwd(), "--repo")) -> None:
    """Revert the last BAIRD commit (uses `git revert`, never rewrites history)."""
    from .diff_apply import DiffApplyError, undo_last_baird_commit

    try:
        new_sha = undo_last_baird_commit(repo)
    except DiffApplyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    console.print(f"[green]reverted, new HEAD[/green] {new_sha[:12]}")


# ----- orchestrator -----


@orchestrator_app.command("serve")
def orchestrator_serve(
    interval: float = typer.Option(2.0, "--tick", help="Scheduler tick seconds"),
    max_workers: int = typer.Option(3, "--max-workers"),
) -> None:
    """Run the background-agent scheduler on the hub.

    Loads tasks from `~/.baird/tasks/*.yaml`, fires on each trigger, enforces
    budgets, posts notifications. Blocks until SIGINT/SIGTERM."""
    import os
    import signal

    from .config import load_hub_config
    from .model import OpenRouterClient
    from .notifier import Notifier, TelegramConfig, TelegramHTTPTransport
    from .scheduler import Scheduler
    from .tasks import load_tasks_dir

    hub_cfg = load_hub_config()
    tasks = load_tasks_dir(_tasks_dir())
    console.print(f"[green]starting orchestrator[/green] tasks={len(tasks)} ceiling=${hub_cfg.daily_total_usd}/day")

    telegram = None
    transport = None
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        telegram = TelegramConfig(bot_token=tg_token, chat_id=tg_chat)
        transport = TelegramHTTPTransport(tg_token)

    with _hub_client_from_host() as hub:
        notifier = Notifier(hub=hub, telegram=telegram, transport=transport)
        scheduler = Scheduler(
            hub=hub,
            model_client=OpenRouterClient(),
            notifier=notifier,
            hub_cfg=hub_cfg,
            host_id=os.uname().nodename,
            max_workers=max_workers,
            tick_seconds=interval,
        )
        scheduler.set_tasks(tasks)
        signal.signal(signal.SIGTERM, lambda *_: scheduler.stop())
        signal.signal(signal.SIGINT, lambda *_: scheduler.stop())
        scheduler.run()


# ----- pipelines / research / improve -----


@app.command()
def snakemake(
    workflow: Path = typer.Argument(..., exists=True, readable=True),
    cwd: Path = typer.Option(None, "--cwd"),
    project: str | None = typer.Option(None, "--project"),
    live: bool = typer.Option(
        False, "--live", help="Stream progress; post a logged inbox row every 10%"
    ),
    extra: list[str] = typer.Argument(None, help="Extra snakemake args (after --)"),
) -> None:
    """Run a Snakemake workflow and post the result back to the hub."""
    from .pipelines import snakemake_run

    with _hub_client_from_host() as hub:
        res = snakemake_run(
            workflow=workflow,
            extra_args=list(extra or []),
            cwd=cwd,
            hub=hub,
            project_id=project,
            live=live,
        )
    console.print(res.summary)
    raise typer.Exit(res.exit_code)


@app.command()
def nextflow(
    workflow: Path = typer.Argument(..., exists=True, readable=True),
    cwd: Path = typer.Option(None, "--cwd"),
    project: str | None = typer.Option(None, "--project"),
    extra: list[str] = typer.Argument(None, help="Extra nextflow args (after --)"),
) -> None:
    """Run a Nextflow workflow and post the result back to the hub."""
    from .pipelines import nextflow_run

    with _hub_client_from_host() as hub:
        res = nextflow_run(
            workflow=workflow,
            extra_args=list(extra or []),
            cwd=cwd,
            hub=hub,
            project_id=project,
        )
    console.print(res.summary)
    raise typer.Exit(res.exit_code)


@app.command()
def improve(
    since_hours: int = typer.Option(24, "--since-hours"),
    model: str = typer.Option("anthropic/claude-3.5-sonnet", "--model"),
) -> None:
    """Fire one self-improvement cycle now."""
    from .model import OpenRouterClient
    from .notifier import Notifier
    from .self_improve import run_self_improvement

    with _hub_client_from_host() as hub:
        notifier = Notifier(hub=hub, telegram=None)
        res = run_self_improvement(
            hub=hub,
            model_client=OpenRouterClient(),
            notifier=notifier,
            since_hours=since_hours,
            model=model,
            tasks_dir=_tasks_dir(),
        )
    console.print(
        f"[green]done[/green] proposals={len(res.proposals)} cost=${res.cost_usd:.4f}"
    )


@app.command()
def research(
    query: str = typer.Argument(...),
    project: str | None = typer.Option(None, "--project"),
    model: str = typer.Option("anthropic/claude-3-haiku", "--model"),
    no_mcp: bool = typer.Option(
        False, "--no-mcp", help="Skip configured MCP servers; web search only."
    ),
) -> None:
    """Run one research cycle for `query` and write the brief to the inbox.

    Uses any MCP servers configured in `~/.baird/mcp_servers.yaml` alongside
    the default Tavily web search. The planner picks per-sub-query which tool
    to use; use `--no-mcp` to stay on web only.
    """
    from .model import OpenRouterClient
    from .notifier import Notifier
    from .research import run_research

    mcp_servers = None
    if not no_mcp:
        from .mcp_client import load_servers
        mcp_servers = load_servers()
        if mcp_servers:
            console.print(
                f"[dim]MCP: {len(mcp_servers)} server(s) available "
                f"({', '.join(s.id for s in mcp_servers)})[/dim]"
            )

    with _hub_client_from_host() as hub:
        notifier = Notifier(hub=hub, telegram=None)
        res = run_research(
            query=query,
            hub=hub,
            model_client=OpenRouterClient(),
            notifier=notifier,
            project_id=project,
            model=model,
            mcp_servers=mcp_servers,
        )
    console.print(res.synthesis)
    console.print(
        f"\n[dim]sub_questions={len(res.sub_questions)}  hits={len(res.hits)}  cost=${res.cost_usd:.4f}[/dim]"
    )


# ----- session (tmux/screen) -----


def _multiplexer():
    """Build a Multiplexer from host.yaml's session_multiplexer setting."""
    from .session_mux import select_backend
    from . import paths as _paths

    host_path = _paths.host_yaml_path()
    pref = "auto"
    if host_path.exists():
        try:
            host_cfg = load_host_config(host_path)
            pref = host_cfg.session_multiplexer or "auto"
        except Exception:
            pass
    return select_backend(pref)


@session_app.command("list")
def session_list() -> None:
    """List multiplexer sessions on this host."""
    mux = _multiplexer()
    sessions = mux.list_sessions()
    if not sessions:
        console.print(f"[dim]no {mux.backend} sessions[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("backend")
    table.add_column("pid")
    for s in sessions:
        table.add_row(s.name, s.backend, str(s.pid) if s.pid is not None else "-")
    console.print(table)


@session_app.command("attach")
def session_attach(name: str) -> None:
    """Print the command to attach to the named session. Run via `eval $(baird session attach <name>)`."""
    mux = _multiplexer()
    cmd = mux.attach_cmd(name=name)
    console.print(" ".join(cmd))


@session_app.command("kill")
def session_kill(name: str) -> None:
    mux = _multiplexer()
    ok = mux.kill(name=name)
    if ok:
        console.print(f"[green]killed[/green] {name}")
    else:
        console.print(f"[red]failed to kill[/red] {name}")
        raise typer.Exit(1)


# ----- daemon -----


@app.command()
def daemon() -> None:
    """Run the satellite-side daemon (watchdog + executor)."""
    from .daemon import main as daemon_main

    raise typer.Exit(daemon_main())


@app.command()
def emit(
    event: str = typer.Argument(..., help="Event name, e.g. pipeline.done"),
    payload: str | None = typer.Option(None, "--payload", help="JSON object"),
) -> None:
    """Publish a reactive event. The scheduler picks it up on the next tick."""
    import json as _json

    data = _json.loads(payload) if payload else None
    with _hub_client_from_host() as hub:
        result = hub.emit_event(event, data)
    console.print(f"[green]emitted[/green] {event}  id={result['id'][:8]}")


files_app = typer.Typer(help="File lineage + registry helpers")
app.add_typer(files_app, name="files")

mcp_app = typer.Typer(help="Manage MCP servers used by `baird research`")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("list")
def mcp_list() -> None:
    """Show MCP servers configured in ~/.baird/mcp_servers.yaml + a tool count
    per server (lazy probes each)."""
    from rich.table import Table

    from .mcp_client import list_tools, load_servers

    servers = load_servers()
    if not servers:
        console.print(
            "[dim]no MCP servers configured. Add some to "
            f"{Path('~/.baird/mcp_servers.yaml').expanduser()}[/dim]"
        )
        return
    t = Table(title="MCP servers")
    t.add_column("id"); t.add_column("command"); t.add_column("tools"); t.add_column("description")
    for s in servers:
        try:
            n = str(len(list_tools(s, timeout=5.0)))
        except Exception:
            n = "?"
        t.add_row(s.id, f"{s.command} {' '.join(s.args)}", n, s.description or "")
    console.print(t)


@mcp_app.command("tools")
def mcp_tools(server_id: str = typer.Argument(...)) -> None:
    """List tools exposed by one MCP server."""
    from .mcp_client import find_server, list_tools

    spec = find_server(server_id)
    if spec is None:
        console.print(f"[red]no MCP server {server_id}[/red]")
        raise typer.Exit(1)
    for t in list_tools(spec):
        console.print(f"[cyan]{t.name}[/cyan]  [dim]{t.description}[/dim]")


@mcp_app.command("call")
def mcp_call(
    server_id: str = typer.Argument(...),
    tool: str = typer.Argument(...),
    args: str = typer.Option("{}", "--args", help="JSON arguments dict"),
) -> None:
    """One-shot tool call for debugging."""
    import json as _json

    from .mcp_client import call_tool, find_server

    spec = find_server(server_id)
    if spec is None:
        console.print(f"[red]no MCP server {server_id}[/red]")
        raise typer.Exit(1)
    out = call_tool(spec, tool, _json.loads(args))
    console.print(out or "[dim](empty result)[/dim]")


@mcp_app.command("ping")
def mcp_ping(server_id: str = typer.Argument(...)) -> None:
    """Reachability check for one server."""
    from .mcp_client import find_server, ping

    spec = find_server(server_id)
    if spec is None:
        console.print(f"[red]no MCP server {server_id}[/red]")
        raise typer.Exit(1)
    if ping(spec):
        console.print(f"[green]ok[/green] {server_id} responded")
    else:
        console.print(f"[red]down[/red] {server_id} did not respond")
        raise typer.Exit(1)


# ----- recall flag / resolve -----


@app.command()
def flag(
    action_id: str = typer.Argument(..., help="Full action id to promote"),
    text: str | None = typer.Option(
        None, "--text",
        help="Text snippet to index. Defaults to the action's summary.",
    ),
    project_id: str | None = typer.Option(None, "--project"),
) -> None:
    """Promote an action snippet to tier-3 in the semantic recall index.

    Use this when an action's summary contains something you want to be sure
    /recall surfaces later (a hard-won lesson, a finicky command, a key
    decision baked into output).
    """
    with _hub_client_from_host() as hub:
        action = hub.get_action(action_id)
        body = text or (action.get("summary") or "").strip()
        if not body:
            console.print("[red]nothing to flag — action has no summary[/red]")
            raise typer.Exit(1)
        r = hub.flag_action(
            action_id=action_id, text=body, project_id=project_id or action.get("project_id")
        )
    if r is None:
        console.print("[yellow]recall index not configured — nothing was indexed[/yellow]")
    else:
        console.print(f"[green]flagged[/green] {action_id[:8]}  fragment={r['id'][:8]}")


@app.command()
def resolve(
    error_action: str = typer.Argument(..., help="Action that failed"),
    fix_action: str = typer.Argument(..., help="Action that fixed it"),
    project_id: str | None = typer.Option(None, "--project"),
) -> None:
    """Promote an error→fix pair into the recall index.

    Stores the error's summary + the fix's summary as one tier-3 fragment so
    /recall surfaces the pair next time something similar fails.
    """
    with _hub_client_from_host() as hub:
        err = hub.get_action(error_action)
        fix = hub.get_action(fix_action)
        body = (
            f"ERROR ({error_action[:8]}): {(err.get('summary') or '').strip()}\n"
            f"FIX ({fix_action[:8]}): {(fix.get('summary') or '').strip()}"
        )
        r = hub.resolve_pair(
            error_action_id=error_action,
            fix_action_id=fix_action,
            text=body,
            project_id=project_id or err.get("project_id"),
        )
    if r is None:
        console.print("[yellow]recall index not configured — nothing was indexed[/yellow]")
    else:
        console.print(
            f"[green]resolved[/green] {error_action[:8]} → {fix_action[:8]}  "
            f"fragment={r['id'][:8]}"
        )


@files_app.command("lineage")
def files_lineage(file_id: str = typer.Argument(...)) -> None:
    """Show the chain of actions that produced (or modified) a file."""
    from rich.table import Table

    with _hub_client_from_host() as hub:
        data = hub.file_lineage(file_id)
    t = Table(title=f"lineage of {data['file_id']}")
    t.add_column("action_id"); t.add_column("role"); t.add_column("tool"); t.add_column("command")
    for a in data.get("actions", []):
        t.add_row(
            a["action_id"][:8],
            a["role"],
            a.get("tool_name", "") or "",
            (a.get("command") or "")[:60],
        )
    console.print(t)


# ----- satellite -----


@satellite_app.command("enroll")
def satellite_enroll(
    ssh_host: str = typer.Argument(..., help="SSH alias (uses ~/.ssh/config) or user@host"),
    host_id: str | None = typer.Option(None, "--host-id", help="BAIRD host_id; defaults to ssh-host"),
    git_ref: str = typer.Option("main", "--git-ref", help="Tag or branch to install on the satellite"),
    port: int | None = typer.Option(None, "--port", help="Hub-side forward port; auto-picked if omitted"),
    watch_root: str = typer.Option("~/projects", "--watch-root"),
    use_hub_for_models: bool = typer.Option(
        True, "--use-hub-for-models/--no-use-hub-for-models",
        help="Route OpenRouter calls via the hub proxy (recommended).",
    ),
) -> None:
    """One-shot satellite setup: SSH out, install BAIRD, write host.yaml,
    stand up the SSH tunnel locally, verify the round-trip."""
    from .satellite import enroll, enroll_spec_from_local

    spec = enroll_spec_from_local(ssh_host, host_id=host_id, git_ref=git_ref)
    spec.local_fwd_port = port
    spec.remote_watch_root = watch_root
    spec.use_hub_for_models = use_hub_for_models

    console.print(f"[green]enrolling[/green] {ssh_host} (host_id={spec.host_id})…")
    res = enroll(spec)
    if res.health_ok:
        console.print(
            f"[green]ok[/green] {res.ssh_host}  port={res.local_fwd_port}  "
            f"home={res.remote_home}"
        )
        console.print(
            f"[dim]tunnel: systemctl --user status baird-tunnel@{ssh_host}[/dim]"
        )
    else:
        console.print(f"[red]enrolment failed[/red]\n{res.detail}")
        raise typer.Exit(1)


@satellite_app.command("list")
def satellite_list() -> None:
    """List enrolled satellites and their tunnel status."""
    from rich.table import Table

    from .satellite import load_registry, tunnel_status

    reg = load_registry()
    if not reg:
        console.print("[dim]no satellites enrolled[/dim]")
        return
    t = Table(title="satellites")
    t.add_column("host_id"); t.add_column("ssh_host"); t.add_column("port")
    t.add_column("hub-for-models"); t.add_column("tunnel")
    for host_id, entry in sorted(reg.items()):
        t.add_row(
            host_id,
            entry.get("ssh_host", ""),
            str(entry.get("local_fwd_port", "")),
            "yes" if entry.get("use_hub_for_models") else "no",
            tunnel_status(entry.get("ssh_host", "")),
        )
    console.print(t)


@satellite_app.command("remove")
def satellite_remove(
    host_id: str = typer.Argument(..., help="host_id from `baird satellite list`")
) -> None:
    """Tear down the hub-side tunnel for a satellite. Leaves the remote
    install in place (you can re-enroll later or clean it manually)."""
    from .satellite import (
        TunnelSpec,
        load_registry,
        remove_tunnel,
        save_registry,
    )

    reg = load_registry()
    entry = reg.get(host_id)
    if not entry:
        console.print(f"[yellow]{host_id} not enrolled[/yellow]")
        raise typer.Exit(1)
    spec = TunnelSpec(
        ssh_host=entry["ssh_host"], local_fwd_port=entry["local_fwd_port"]
    )
    remove_tunnel(spec)
    del reg[host_id]
    save_registry(reg)
    console.print(f"[green]removed[/green] {host_id}")


@satellite_app.command("restart-daemon")
def satellite_restart_daemon(
    host_id: str = typer.Argument(..., help="host_id from `baird satellite list`"),
    port: int = typer.Option(8765, "--port", "-p", help="Executor port on the satellite"),
) -> None:
    """Restart the baird daemon on a satellite via SSH.

    Kills any process listening on the executor port, then starts the daemon
    in the background with nohup. Works around procps bugs on some HPC hosts
    (hibu) by using `fuser` instead of `ps`/`pkill`.
    """
    import subprocess as _subprocess

    from .satellite import load_registry

    reg = load_registry()
    entry = reg.get(host_id)
    if not entry:
        console.print(f"[yellow]{host_id} not enrolled[/yellow]")
        raise typer.Exit(1)
    ssh_host = entry["ssh_host"]
    remote_dir = entry.get("remote_baird_dir", "$HOME/code/BAIRD")

    console.print(f"[cyan]restarting daemon on {host_id} ({ssh_host})…[/cyan]")

    kill_script = (
        f"fuser -k {port}/tcp 2>/dev/null; "
        f"echo kill_exit=$?"
    )
    r = _subprocess.run(
        ["ssh", "-o", "BatchMode=yes", ssh_host, kill_script],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        console.print(f"[yellow]kill step had issues: {r.stderr.strip() or r.stdout.strip()}[/yellow]")
    else:
        console.print(f"[green]killed old daemon on port {port}[/green]")

    import time
    time.sleep(1)

    start_script = (
        f"cd {remote_dir} && "
        "nohup env PATH=\"$HOME/.local/bin:$PATH\" "
        "\"$HOME/.local/bin/uv\" run python -m baird.daemon "
        "</dev/null >/tmp/baird-daemon.log 2>&1 & "
        "disown; echo daemon_started=$?"
    )
    try:
        r2 = _subprocess.run(
            ["ssh", "-o", "BatchMode=yes", ssh_host, start_script],
            capture_output=True, text=True, timeout=15,
        )
        if r2.returncode != 0:
            console.print(f"[red]start step failed[/red]\n{r2.stderr.strip() or r2.stdout.strip()}")
            raise typer.Exit(1)
        console.print(f"[green]daemon started on {host_id}[/green]")
    except _subprocess.TimeoutExpired:
        console.print("[yellow]start command timed out (daemon may still be booting)[/yellow]")

    time.sleep(3)
    console.print(f"[dim]check: systemctl --user status baird-tunnel@{ssh_host}[/dim]")
    console.print(f"[dim]log: ssh {ssh_host} 'tail -20 /tmp/baird-daemon.log'[/dim]")


@satellite_app.command("doctor")
def satellite_doctor(
    host_id: str = typer.Argument(..., help="host_id from `baird satellite list`"),
    port: int = typer.Option(8765, "--port", "-p", help="Default executor port on the satellite"),
) -> None:
    """Diagnose and fix a satellite: checks SSH, tunnel, daemon, port
    alignment, and round-trip connectivity.

    Each check shows ✅ (pass) / ❌ (fail) / 🔧 (fixed). Fixes are applied
    automatically so the satellite reaches a healthy state.
    """
    import subprocess as sp
    import time

    from .satellite import (
        TunnelSpec,
        install_tunnel,
        load_registry,
        tunnel_status,
    )

    checks: list[dict] = []

    def ok(msg: str) -> None:
        checks.append({"status": "ok", "msg": msg})

    def fail(msg: str) -> None:
        checks.append({"status": "fail", "msg": msg})

    def fix(msg: str) -> None:
        checks.append({"status": "fix", "msg": msg})

    def _ssh(cmd: str, *, timeout: float = 10) -> sp.CompletedProcess:
        return sp.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", ssh_host, cmd],
            capture_output=True, text=True, timeout=timeout,
        )

    # ---- 1. Registry ----
    reg = load_registry()
    entry = reg.get(host_id)
    if not entry:
        console.print(f"[red]❌ host_id {host_id!r} not in satellite registry[/red]")
        raise typer.Exit(1)
    ssh_host = entry["ssh_host"]
    fwd_port = entry.get("local_fwd_port", 8766)
    remote_dir = entry.get("remote_baird_dir", "$HOME/code/BAIRD")
    token = entry.get("executor_auth_token", "")
    ok(f"registry: {host_id} → ssh {ssh_host}, fwd port {fwd_port}")

    # ---- 2. SSH ----
    r = _ssh("echo pong", timeout=8)
    if r.returncode != 0:
        fail(f"SSH to {ssh_host}: {r.stderr.strip() or 'no route'}")
        console.print("[red]✗ SSH is down — cannot proceed[/red]")
        raise typer.Exit(1)
    ok(f"SSH to {ssh_host}")

    # ---- 3. Tunnel unit + env file ----
    tspec = TunnelSpec(ssh_host=ssh_host, local_fwd_port=fwd_port, satellite_port=port)
    env_file = tspec.baird_config_dir / f"tunnel-{ssh_host}.env"
    unit_file = tspec.systemd_user_dir / "baird-tunnel@.service"
    missing = []
    if not unit_file.exists():
        missing.append("unit file")
    if not env_file.exists():
        missing.append("env file")
    if missing:
        fix(f"tunnel files missing: {', '.join(missing)} — installing")
        install_tunnel(tspec)
        time.sleep(1)
    else:
        ok("tunnel unit + env file present")

    # ---- 4. Tunnel active ----
    status = tunnel_status(ssh_host)
    if status != "active":
        fix(f"tunnel is {status} — restarting")
        _tunnel_cmd("restart", ssh_host)
        time.sleep(2)
        status2 = tunnel_status(ssh_host)
        if status2 != "active":
            fail(f"tunnel still {status2} after restart")
        else:
            ok("tunnel active")
    else:
        ok("tunnel active")

    # ---- 5. Daemon running on satellite ----
    r = _ssh(f"ss -tlnp 'sport = :{port}'", timeout=10)
    daemon_on_port = bool(r.stdout.strip())
    if not daemon_on_port:
        fix(f"daemon not listening on port {port} — starting")
        start_script = (
            f"cd {remote_dir} && "
            "nohup env PATH=\"$HOME/.local/bin:$PATH\" "
            "\"$HOME/.local/bin/uv\" run python -m baird.daemon "
            "</dev/null >/tmp/baird-daemon.log 2>&1 & "
            "disown; echo started=$?"
        )
        try:
            r2 = _ssh(start_script, timeout=15)
            if r2.returncode != 0:
                fail(f"daemon start failed: {r2.stderr.strip() or r2.stdout.strip()}")
            else:
                time.sleep(3)
                r3 = _ssh(f"ss -tlnp 'sport = :{port}'", timeout=10)
                if r3.stdout.strip():
                    ok("daemon listening on port 8765")
                else:
                    actual = _ssh(
                        "ss -tlnp 'sport >= :8765 and sport <= :8775'", timeout=10
                    )
                    lines = [l for l in actual.stdout.splitlines() if "LISTEN" in l]
                    if lines:
                        for ln in lines[:3]:
                            console.print(f"  [dim]listening: {ln.strip()}[/dim]")
                        fix("daemon running on alternate port — see above")
                    else:
                        fail("daemon failed to start")
        except sp.TimeoutExpired:
            fail("start command timed out")
    else:
        ok("daemon listening")

    # ---- 6. Round-trip health check ----
    if fwd_port and token:
        try:
            from .executor_client import ExecutorClient

            with ExecutorClient(f"http://127.0.0.1:{fwd_port}", token, timeout=5) as ec:
                h = ec.health()
            ok(f"executor health: {h}")
        except Exception as e:
            fail(f"executor health check: {e}")
    else:
        fail("cannot check health — missing fwd_port or token")

    # ---- Summary ----
    console.print()
    for c in checks:
        icon = {"ok": "✅", "fail": "❌", "fix": "🔧"}.get(c["status"], "?")
        console.print(f"  {icon} {c['msg']}")
    has_error = any(c["status"] == "fail" for c in checks)
    if has_error:
        console.print(f"\n[yellow]⚠  some checks failed — unresolved issues remain[/yellow]")
        console.print(f"[dim]hint: ssh {ssh_host} 'tail -30 /tmp/baird-daemon.log'[/dim]")
        raise typer.Exit(1)
    else:
        console.print(f"\n[bold green]✅ {host_id} is healthy[/bold green]")


def _tunnel_cmd(action: str, ssh_host: str | None = None) -> None:
    """Helper: systemctl --user <action> baird-tunnel@<host>."""
    import subprocess as sp
    cmd = ["systemctl", "--user", action]
    if ssh_host:
        cmd.append(f"baird-tunnel@{ssh_host}")
    sp.run(cmd, capture_output=True, timeout=30)


# ---- Debug commands -----------------------------------------------------


@app.command()
def debug_session(
    session_id: str = typer.Argument(..., help="Session UUID to inspect"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max messages to show"),
) -> None:
    """Replay a session's message history — shows every persisted turn
    including system prompts, user messages, assistant tool_calls, and
    tool results. Useful for debugging what the model actually saw."""
    from .memory_client import HubClient

    with _hub_client_from_host() as hub:
        msgs = hub.get_messages(session_id, limit=limit)
    if not msgs:
        console.print(f"[yellow]no messages found for session {session_id[:8]}[/yellow]")
        return
    from rich.table import Table
    from rich.text import Text

    console.print(f"[cyan]Session {session_id} — {len(msgs)} message(s)[/cyan]")
    for i, m in enumerate(msgs):
        role = m.get("role", "?")
        content = (m.get("content") or "")[:300]
        tool_calls = m.get("tool_calls")
        tool_call_id = m.get("tool_call_id", "")
        style = {
            "user": "green", "assistant": "blue", "tool": "yellow", "system": "dim",
        }.get(role, "white")
        label = f"[{i}] {role}"
        if tool_call_id:
            label += f" ↦ {tool_call_id[:12]}"
        if tool_calls:
            names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
            content = f"tool_calls: {', '.join(names)} | args: ..."
        console.print(Text(f"{label}", style=style))
        console.print(Text(f"  {content[:200]}", style=style))


if __name__ == "__main__":
    app()
