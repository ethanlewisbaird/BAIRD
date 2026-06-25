"""`baird status` dashboard — Phase 5 design (#3+#4).

A one-shot snapshot of the harness state. Pure data-gathering here; the
rendering happens in cli.py so this module stays test-friendly.

`render_status_dashboard(state)` is the only public function — takes the
gathered `DashboardState` and emits Rich renderables, but as console.print
calls inline rather than returning a Layout. (We're deferring the live TUI
to a later slice — the design says ship the one-shot first.)
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import HubConfig
from .memory_client import HubClient
from .tasks import CronTrigger, IntervalTrigger, Task


@dataclass
class DashboardState:
    hub_url: str
    hub_ok: bool
    stats: dict = field(default_factory=dict)
    budget_today_usd: float = 0.0
    budget_ceiling_usd: float = 0.0
    recent_actions: list[dict] = field(default_factory=list)
    inbox_unresolved: list[dict] = field(default_factory=list)
    tasks: dict[str, Task] = field(default_factory=dict)
    last_firings: dict[str, dict] = field(default_factory=dict)  # task_id → latest Action row
    task_spend_today: dict[str, float] = field(default_factory=dict)
    error: str | None = None


def gather(
    *,
    hub: HubClient,
    hub_cfg: HubConfig,
    tasks: dict[str, Task],
    recent_n: int = 10,
    inbox_n: int = 5,
) -> DashboardState:
    """Collect everything needed for a dashboard render. Network calls happen
    here — keep the caller free to test rendering without hitting a hub."""
    state = DashboardState(
        hub_url=getattr(hub._client, "base_url", "<unknown>") and str(hub._client.base_url),  # type: ignore[attr-defined]
        hub_ok=False,
        budget_ceiling_usd=hub_cfg.daily_total_usd,
        tasks=tasks,
    )

    try:
        hub.health()
        state.hub_ok = True
    except Exception as e:
        state.error = str(e)
        return state

    state.stats = hub.stats()
    state.budget_today_usd = hub.budgets_usage(since_hours=24)["cost_usd"]
    state.recent_actions = hub.list_actions(limit=recent_n)
    state.inbox_unresolved = hub.list_notifications(unresolved_only=True, limit=inbox_n)

    for tid in tasks:
        latest = hub.list_actions(task_id=tid, limit=1)
        if latest:
            state.last_firings[tid] = latest[0]
        state.task_spend_today[tid] = hub.budgets_usage(since_hours=24, task_id=tid)["cost_usd"]

    return state


# ---- Rendering ---------------------------------------------------------


def render(state: DashboardState, console: Console) -> None:
    if not state.hub_ok:
        console.print(
            Panel.fit(
                f"[red]hub unreachable[/red] at {state.hub_url}\n{state.error or ''}",
                title="status",
                border_style="red",
            )
        )
        return

    console.print(
        Panel.fit(
            f"[green]hub OK[/green]  {state.hub_url}",
            title="status",
            border_style="green",
        )
    )

    console.print(_counts_panel(state))
    console.print(_budget_panel(state))
    console.print(_inbox_panel(state))
    console.print(_recent_actions_panel(state))
    if state.tasks:
        console.print(_tasks_panel(state))


def _counts_panel(state: DashboardState) -> Panel:
    s = state.stats
    body = (
        f"files live:        {s.get('files_live', 0)}\n"
        f"actions total:     {s.get('actions_total', 0)}  "
        f"(running: {s.get('actions_running', 0)})\n"
        f"projects:          {s.get('projects', 0)}\n"
        f"decisions:         {s.get('decisions', 0)}\n"
        f"inbox unresolved:  {s.get('notifications_unresolved', 0)}"
    )
    return Panel(body, title="registry + memory", border_style="cyan", expand=False)


def _budget_panel(state: DashboardState) -> Panel:
    pct = (
        state.budget_today_usd / state.budget_ceiling_usd
        if state.budget_ceiling_usd > 0
        else 0.0
    )
    color = "green" if pct < 0.5 else ("yellow" if pct < 0.9 else "red")
    body = (
        f"today: [{color}]${state.budget_today_usd:.4f}[/{color}] / "
        f"${state.budget_ceiling_usd:.2f}  ({pct * 100:.1f}%)"
    )
    return Panel(body, title="budget", border_style=color, expand=False)


def _inbox_panel(state: DashboardState) -> Panel:
    if not state.inbox_unresolved:
        return Panel("[dim](none)[/dim]", title="inbox (unresolved)", border_style="blue", expand=False)
    table = Table.grid(padding=(0, 2))
    table.add_column("id", style="dim")
    table.add_column("kind")
    table.add_column("title")
    for n in state.inbox_unresolved:
        table.add_row(n["id"][:8], n["kind"], n["title"])
    return Panel(table, title="inbox (unresolved)", border_style="blue", expand=False)


def _recent_actions_panel(state: DashboardState) -> Panel:
    if not state.recent_actions:
        return Panel("[dim](none)[/dim]", title="recent actions", border_style="magenta", expand=False)
    table = Table.grid(padding=(0, 2))
    table.add_column("started", style="dim")
    table.add_column("id", style="dim")
    table.add_column("project")
    table.add_column("tool/cmd")
    table.add_column("status")
    for a in state.recent_actions:
        status = "…" if a.get("finished_at") is None else f"exit {a.get('exit_code')}"
        table.add_row(
            (a.get("started_at") or "")[:19],
            (a.get("id") or "")[:8],
            a.get("project_id") or "-",
            (a.get("command") or a.get("tool_name") or "?")[:40],
            status,
        )
    return Panel(table, title="recent actions", border_style="magenta", expand=False)


def _tasks_panel(state: DashboardState) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column("id")
    table.add_column("trigger")
    table.add_column("enabled")
    table.add_column("last fire")
    table.add_column("today spend")
    for tid, task in state.tasks.items():
        last = state.last_firings.get(tid)
        spend = state.task_spend_today.get(tid, 0.0)
        table.add_row(
            tid,
            _trigger_repr(task),
            "yes" if task.enabled else "no",
            (last["started_at"][:19] if last else "—"),
            f"${spend:.4f}",
        )
    return Panel(table, title="tasks", border_style="yellow", expand=False)


def _trigger_repr(task: Task) -> str:
    trig = task.trigger
    if isinstance(trig, CronTrigger):
        return f"cron({trig.cron})"
    if isinstance(trig, IntervalTrigger):
        return f"every({trig.interval_seconds}s)"
    return trig.type


def render_age(dt_iso: str | None) -> str:
    if not dt_iso:
        return "—"
    try:
        when = dt.datetime.fromisoformat(dt_iso.rstrip("Z"))
    except ValueError:
        return dt_iso
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    delta = dt.datetime.now(dt.timezone.utc) - when
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86_400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86_400}d ago"
