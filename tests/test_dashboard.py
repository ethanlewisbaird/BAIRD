"""Tests for the dashboard's gather() pipeline + render() smoke."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from rich.console import Console

from baird.config import HubConfig
from baird.dashboard import DashboardState, gather, render
from baird.memory_client import HubClient
from baird.tasks import IntervalTrigger, Runnable, Task


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


@pytest.fixture
def hub(client: TestClient) -> _Hub:
    return _Hub(client)


def test_gather_empty_hub(hub: _Hub) -> None:
    state = gather(hub=hub, hub_cfg=HubConfig(daily_total_usd=1.0), tasks={})
    assert state.hub_ok
    assert state.stats["files_live"] == 0
    assert state.budget_today_usd == 0.0
    assert state.recent_actions == []
    assert state.inbox_unresolved == []


def test_gather_with_activity(hub: _Hub, client: TestClient) -> None:
    client.post("/projects", json={"id": "p", "name": "p"})
    with hub.start_action(task_id="t1", command="ping", host="h") as h:
        h.record_usage(cost_usd=0.05, input_tokens=10, output_tokens=10)
    client.post("/notifications", json={"kind": "approval", "title": "needs approve"})

    task = Task(id="t1", trigger=IntervalTrigger(interval_seconds=60), runnable=Runnable(prompt="x"))
    state = gather(hub=hub, hub_cfg=HubConfig(daily_total_usd=1.0), tasks={"t1": task})
    assert state.stats["projects"] == 1
    assert state.budget_today_usd == pytest.approx(0.05)
    assert state.recent_actions  # at least one
    assert state.inbox_unresolved  # one approval
    assert state.last_firings.get("t1") is not None
    assert state.task_spend_today["t1"] == pytest.approx(0.05)


def test_render_does_not_crash_on_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Build a DashboardState manually to exercise the unreachable branch.
    state = DashboardState(
        hub_url="http://nope",
        hub_ok=False,
        error="connection refused",
    )
    console = Console(record=True, width=80)
    render(state, console)
    assert "hub unreachable" in console.export_text()


def test_render_full_state(hub: _Hub, client: TestClient) -> None:
    client.post("/projects", json={"id": "p", "name": "p"})
    with hub.start_action(task_id="t1", command="ping", host="h") as h:
        h.record_usage(cost_usd=0.01, input_tokens=5, output_tokens=5)
    task = Task(id="t1", trigger=IntervalTrigger(interval_seconds=60), runnable=Runnable(prompt="x"))
    state = gather(hub=hub, hub_cfg=HubConfig(daily_total_usd=1.0), tasks={"t1": task})

    console = Console(record=True, width=120)
    render(state, console)
    text = console.export_text()
    assert "hub OK" in text
    assert "registry + memory" in text
    assert "budget" in text
    assert "tasks" in text
