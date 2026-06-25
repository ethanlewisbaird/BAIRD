"""BAIRD CLI entry — `baird` command.

Minimum-viable surface from the Phase 5 design:
- `baird` / `baird code` — interactive coding shell (not yet implemented)
- `baird project pull / push / list` — project management (stubs)
- `baird task run` — fire a background task once (stub)
- `baird inbox` — show notifications (stub)
- `baird hub serve` — run the FastAPI hub service (works)
- `baird daemon` — run the satellite daemon (scaffolded)

The full surface from the design grows in as features land.
"""

from __future__ import annotations

import typer
from rich.console import Console

from . import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False, help="BAIRD — Bioinformatics AI Research Daemon")
project_app = typer.Typer(help="Project management")
task_app = typer.Typer(help="Background task management")
hub_app = typer.Typer(help="Hub service")

app.add_typer(project_app, name="project")
app.add_typer(task_app, name="task")
app.add_typer(hub_app, name="hub")

console = Console()


@app.callback()
def main(version: bool = typer.Option(False, "--version", help="Show version and exit")) -> None:
    if version:
        console.print(f"baird {__version__}")
        raise typer.Exit()


@app.command()
def code(project: str | None = typer.Option(None, "--project", help="Project id (auto-detected if omitted)")) -> None:
    """Interactive coding mode (not yet implemented)."""
    console.print("[yellow]`baird code` is not yet implemented (Phase 3)[/yellow]")
    if project:
        console.print(f"requested project: {project}")
    raise typer.Exit(1)


@app.command()
def chat() -> None:
    """Interactive chat mode, no repo context (not yet implemented)."""
    console.print("[yellow]`baird chat` is not yet implemented (Phase 3)[/yellow]")
    raise typer.Exit(1)


@app.command()
def inbox(since: str | None = typer.Option(None, "--since", help="e.g. 1d, 7d")) -> None:
    """Show notifications (not yet implemented)."""
    console.print("[yellow]`baird inbox` is not yet implemented (Phase 4)[/yellow]")
    if since:
        console.print(f"since: {since}")
    raise typer.Exit(1)


# ----- project -----


@project_app.command("pull")
def project_pull(project_id: str, to: str = typer.Option(..., "--to", help="Target host")) -> None:
    """Clone a project to a satellite and register the checkout."""
    console.print(f"[yellow]`baird project pull {project_id} --to {to}` not yet implemented (Phase 3)[/yellow]")
    raise typer.Exit(1)


@project_app.command("push")
def project_push(project_id: str | None = typer.Argument(None)) -> None:
    """Push project changes (git push + registry update)."""
    console.print("[yellow]`baird project push` not yet implemented (Phase 3)[/yellow]")
    raise typer.Exit(1)


@project_app.command("list")
def project_list() -> None:
    """List known projects."""
    console.print("[yellow]`baird project list` not yet implemented (Phase 2)[/yellow]")
    raise typer.Exit(1)


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


# ----- daemon -----


@app.command()
def daemon() -> None:
    """Run the satellite-side daemon (watchdog + executor)."""
    from .daemon import main as daemon_main

    raise typer.Exit(daemon_main())


if __name__ == "__main__":
    app()
