"""BAIRD CLI entry — `baird` command.

Minimum-viable surface from the Phase 5 design. Phase 2 wires up:
- `baird project init / push / pull / list`
- `baird inbox list / resolve`

Coding mode, chat, and task execution still wait on later phases.
"""

from __future__ import annotations

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
    no_args_is_help=True,
    add_completion=False,
    invoke_without_command=True,
    help="BAIRD — Bioinformatics AI Research Daemon",
)
project_app = typer.Typer(help="Project management")
task_app = typer.Typer(help="Background task management")
hub_app = typer.Typer(help="Hub service")
inbox_app = typer.Typer(help="Notification inbox", invoke_without_command=True)
diff_app = typer.Typer(help="Diff review/apply")

app.add_typer(project_app, name="project")
app.add_typer(task_app, name="task")
app.add_typer(hub_app, name="hub")
app.add_typer(inbox_app, name="inbox")
app.add_typer(diff_app, name="diff")

console = Console()


# ----- shared helpers -----


def _hub_client_from_host() -> HubClient:
    """Build a HubClient from ~/.baird/host.yaml (the satellite-side config).

    Falls back to ~/.baird/config.yaml's `listen` if host.yaml is missing,
    so this also works on the hub itself for local CLI use.
    """
    host_path = Path("~/.baird/host.yaml").expanduser()
    if host_path.exists():
        host_cfg = load_host_config(host_path)
        return HubClient(host_cfg.hub_url, host_cfg.auth_token)
    hub_cfg = load_hub_config()
    host, port = hub_cfg.listen.split(":")
    return HubClient(f"http://{host}:{port}")


def _project_yaml_path(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / ".baird" / "project.yaml"


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
        console.print(ctx.get_help())
        raise typer.Exit()


@app.command()
def code(
    show_context: bool = typer.Option(
        False, "--show-context", help="Print the rendered repo context and exit"
    ),
    file: list[str] = typer.Option(
        [], "--file", "-f", help="Extra files to include in the context block"
    ),
    budget: int = typer.Option(6000, "--budget", help="Approx token budget for the context block"),
) -> None:
    """Interactive coding mode.

    Phase 3 ships the substrate: this command loads the repo context for the
    current project. The actual LLM call lands in Phase 4 (OpenRouter). Until
    then, `--show-context` is the useful flag.
    """
    from .context_loader import load_repo_context, render_context

    root = Path.cwd()
    if not (root / ".baird" / "project.yaml").exists():
        console.print("[red]no .baird/project.yaml in cwd[/red] — run `baird project init`")
        raise typer.Exit(1)

    try:
        with _hub_client_from_host() as hub:
            ctx = load_repo_context(root, hub=hub)
    except Exception:
        ctx = load_repo_context(root, hub=None)

    if show_context:
        console.print(render_context(ctx, token_budget=budget))
        return

    console.print(
        "[yellow]`baird code` REPL is not yet wired (Phase 4 will add the OpenRouter call).[/yellow]\n"
        "Use `--show-context` to inspect the per-turn context block."
    )


@app.command()
def chat() -> None:
    """Interactive chat mode, no repo context (not yet implemented)."""
    console.print("[yellow]`baird chat` is not yet implemented (Phase 3)[/yellow]")
    raise typer.Exit(1)


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


@task_app.command("run")
def task_run(task_id: str) -> None:
    """Fire a background task once."""
    console.print(f"[yellow]`baird task run {task_id}` not yet implemented (Phase 4)[/yellow]")
    raise typer.Exit(1)


# ----- hub -----


@hub_app.command("serve")
def hub_serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Run the BAIRD hub FastAPI service."""
    import uvicorn

    console.print(f"[green]starting BAIRD hub on {host}:{port}[/green]")
    uvicorn.run("baird.hub:app", host=host, port=port, log_level="info")


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


# ----- daemon -----


@app.command()
def daemon() -> None:
    """Run the satellite-side daemon (watchdog + executor)."""
    from .daemon import main as daemon_main

    raise typer.Exit(daemon_main())


if __name__ == "__main__":
    app()
