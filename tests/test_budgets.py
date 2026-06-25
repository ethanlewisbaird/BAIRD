"""Tests for the budgets module against a real hub."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from baird.budgets import check_task_budget
from baird.config import HubConfig
from baird.memory_client import HubClient
from baird.tasks import Budget, IntervalTrigger, Runnable, Task


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


@pytest.fixture
def hub(client: TestClient) -> _Hub:
    return _Hub(client)


def _record_spend(hub: _Hub, *, task_id: str, cost_usd: float) -> None:
    """Helper: create + finish an action with a recorded cost."""
    with hub.start_action(task_id=task_id, command=f"task:{task_id}", host="t") as h:
        h.record_usage(cost_usd=cost_usd, input_tokens=10, output_tokens=10)


def _task(task_id: str = "t1", max_cost_usd: float | None = 0.10) -> Task:
    return Task(
        id=task_id,
        trigger=IntervalTrigger(interval_seconds=60),
        runnable=Runnable(prompt="hi"),
        budget=Budget(max_cost_usd=max_cost_usd),
    )


def test_under_budget_ok(hub: _Hub) -> None:
    check = check_task_budget(hub=hub, task=_task(), hub_cfg=HubConfig(daily_total_usd=1.0))
    assert check.ok


def test_per_task_cap_blocks(hub: _Hub) -> None:
    _record_spend(hub, task_id="t1", cost_usd=0.15)
    check = check_task_budget(hub=hub, task=_task(max_cost_usd=0.10), hub_cfg=HubConfig(daily_total_usd=1.0))
    assert not check.ok
    assert "per-task" in check.reason


def test_global_ceiling_blocks(hub: _Hub) -> None:
    _record_spend(hub, task_id="other", cost_usd=2.0)
    check = check_task_budget(hub=hub, task=_task(max_cost_usd=10.0), hub_cfg=HubConfig(daily_total_usd=1.0))
    assert not check.ok
    assert "global" in check.reason


def test_disabled_task_blocks(hub: _Hub) -> None:
    t = _task()
    t.enabled = False
    check = check_task_budget(hub=hub, task=t, hub_cfg=HubConfig(daily_total_usd=10.0))
    assert not check.ok
    assert "disabled" in check.reason


def test_per_task_falls_back_to_default(hub: _Hub) -> None:
    """When task has no max_cost_usd, falls back to hub_cfg.daily_per_task_default_usd."""
    _record_spend(hub, task_id="t1", cost_usd=0.6)
    check = check_task_budget(
        hub=hub,
        task=_task(max_cost_usd=None),
        hub_cfg=HubConfig(daily_total_usd=10.0, daily_per_task_default_usd=0.5),
    )
    assert not check.ok
