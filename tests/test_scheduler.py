"""Scheduler tests — fire-time math, budget gate, concurrency."""

from __future__ import annotations

import datetime as dt
import time

import pytest
from fastapi.testclient import TestClient

from baird.config import HubConfig
from baird.memory_client import HubClient
from baird.model import OpenRouterClient
from baird.scheduler import Scheduler, expand_project_ids, next_fire_after
from baird.tasks import (
    Budget,
    CronTrigger,
    IntervalTrigger,
    Runnable,
    Task,
    WatchTrigger,
)


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


def _model_client():
    def t(_req: dict) -> dict:
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "cost": 0.0001},
        }
    return OpenRouterClient(transport=t)


def _task(task_id: str, interval_s: int = 60) -> Task:
    return Task(
        id=task_id,
        trigger=IntervalTrigger(interval_seconds=interval_s),
        runnable=Runnable(prompt="hi", model="anthropic/claude-3-haiku"),
        budget=Budget(max_cost_usd=1.0),
    )


# ---- next_fire_after ---------------------------------------------------


def test_next_fire_after_interval() -> None:
    now = dt.datetime(2026, 6, 25, 12, 0, 0, tzinfo=dt.timezone.utc)
    nf = next_fire_after(_task("t", interval_s=30), now)
    assert nf == now + dt.timedelta(seconds=30)


def test_next_fire_after_cron() -> None:
    now = dt.datetime(2026, 6, 25, 8, 30, 0, tzinfo=dt.timezone.utc)
    task = Task(
        id="t",
        trigger=CronTrigger(cron="0 9 * * *"),
        runnable=Runnable(prompt="x"),
    )
    nf = next_fire_after(task, now)
    assert nf == dt.datetime(2026, 6, 25, 9, 0, 0, tzinfo=dt.timezone.utc)


def test_next_fire_after_disabled_is_none() -> None:
    t = _task("t")
    t.enabled = False
    assert next_fire_after(t, dt.datetime.now(dt.timezone.utc)) is None


def test_next_fire_after_unsupported_trigger_is_none() -> None:
    t = Task(
        id="x",
        trigger=WatchTrigger(path="/tmp"),
        runnable=Runnable(prompt="hi"),
    )
    assert next_fire_after(t, dt.datetime.now(dt.timezone.utc)) is None


# ---- Scheduler tick ----------------------------------------------------


def test_due_interval_task_fires(client: TestClient) -> None:
    hub = _Hub(client)
    sched = Scheduler(
        hub=hub,
        model_client=_model_client(),
        hub_cfg=HubConfig(daily_total_usd=10.0),
        max_workers=2,
        tick_seconds=0.05,
    )
    sched.set_tasks({"a": _task("a", interval_s=1)})
    # Force the task to be already due.
    sched._entries["a"].next_fire = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)

    # Run a few ticks then stop.
    import threading
    th = threading.Thread(target=sched.run, daemon=True)
    th.start()
    time.sleep(0.4)
    sched.stop()
    th.join(timeout=2.0)

    actions = hub.list_actions(task_id="a")
    assert actions, "expected at least one firing"
    assert actions[0]["exit_code"] == 0


def test_budget_block_skips_firing(client: TestClient) -> None:
    hub = _Hub(client)
    # Pre-spend over the global ceiling.
    with hub.start_action(task_id="other", host="h") as h:
        h.record_usage(cost_usd=10.0)

    sched = Scheduler(
        hub=hub,
        model_client=_model_client(),
        hub_cfg=HubConfig(daily_total_usd=1.0),
        tick_seconds=0.05,
    )
    sched.set_tasks({"a": _task("a", interval_s=1)})
    sched._entries["a"].next_fire = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)

    import threading
    th = threading.Thread(target=sched.run, daemon=True)
    th.start()
    time.sleep(0.3)
    sched.stop()
    th.join(timeout=2.0)

    # No firings recorded for task "a".
    assert hub.list_actions(task_id="a") == []


def test_in_flight_task_does_not_re_fire(client: TestClient) -> None:
    """If a firing is still running, the scheduler must not start another one."""
    hub = _Hub(client)

    fire_counter = {"n": 0}

    def slow(_req: dict) -> dict:
        fire_counter["n"] += 1
        time.sleep(0.5)
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0001},
        }

    sched = Scheduler(
        hub=hub,
        model_client=OpenRouterClient(transport=slow),
        hub_cfg=HubConfig(daily_total_usd=10.0),
        tick_seconds=0.05,
    )
    sched.set_tasks({"a": _task("a", interval_s=1)})
    sched._entries["a"].next_fire = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)

    import threading
    th = threading.Thread(target=sched.run, daemon=True)
    th.start()
    time.sleep(0.3)  # plenty of ticks during the in-flight period
    sched.stop()
    th.join(timeout=2.0)

    assert fire_counter["n"] == 1


# ---- expand_project_ids ----------------------------------------------


def test_expand_project_ids_replaces_parent_with_children(client: TestClient) -> None:
    hub = _Hub(client)
    client.post("/projects", json={"id": "scentinel", "name": "S"})
    client.post(
        "/projects",
        json={"id": "scentinel-scrna", "name": "scRNA", "parent_id": "scentinel"},
    )
    client.post(
        "/projects",
        json={"id": "scentinel-spatial", "name": "spatial", "parent_id": "scentinel"},
    )
    out = expand_project_ids(hub, ["scentinel"])
    assert sorted(out) == ["scentinel-scrna", "scentinel-spatial"]


def test_expand_project_ids_leaves_leaf_ids_alone(client: TestClient) -> None:
    hub = _Hub(client)
    client.post("/projects", json={"id": "leaf", "name": "leaf"})
    out = expand_project_ids(hub, ["leaf"])
    assert out == ["leaf"]


def test_expand_project_ids_explicit_list_no_expansion(client: TestClient) -> None:
    """When the caller already supplies a list of leaves, even though
    they happen to share a parent, expand returns them as-is — no
    re-expansion."""
    hub = _Hub(client)
    client.post("/projects", json={"id": "u", "name": "u"})
    client.post("/projects", json={"id": "a", "name": "a", "parent_id": "u"})
    client.post("/projects", json={"id": "b", "name": "b", "parent_id": "u"})
    out = expand_project_ids(hub, ["a", "b"])
    assert out == ["a", "b"]


def test_expand_project_ids_mixed_parent_and_leaf(client: TestClient) -> None:
    """Parents expand; standalone leaves pass through. Duplicates are dedup'd
    (children appearing after a sibling leaf with the same id)."""
    hub = _Hub(client)
    client.post("/projects", json={"id": "u", "name": "u"})
    client.post("/projects", json={"id": "a", "name": "a", "parent_id": "u"})
    client.post("/projects", json={"id": "standalone", "name": "s"})
    out = expand_project_ids(hub, ["u", "standalone", "a"])
    # `u` expands to ["a"]; then "standalone"; then "a" — already seen, dropped.
    assert out == ["a", "standalone"]


def test_expand_project_ids_empty_list() -> None:
    """Pure helper — empty in, empty out, no hub call needed."""
    out = expand_project_ids(hub=None, project_ids=[])  # type: ignore[arg-type]
    assert out == []


# ---- Multi-project firing --------------------------------------------


def test_multi_project_task_fires_once_per_resolved_id(
    client: TestClient, monkeypatch
) -> None:
    """Slice E integration: when runnable.project_ids contains a parent,
    _do_fire substitutes runnable.project_id with each child and invokes
    run_task_once once per id. We monkeypatch run_task_once with a recorder
    so we don't depend on model/session machinery — the property under
    test is the per-id dispatch, not what each firing does internally."""
    hub = _Hub(client)
    client.post("/projects", json={"id": "scentinel", "name": "SCENTINEL"})
    client.post(
        "/projects",
        json={"id": "scentinel-scrna", "name": "scRNA", "parent_id": "scentinel"},
    )
    client.post(
        "/projects",
        json={"id": "scentinel-spatial", "name": "spatial", "parent_id": "scentinel"},
    )

    fired_pids: list[str] = []

    def _recorder(task, **kwargs):
        fired_pids.append(task.runnable.project_id or "")
        return None

    monkeypatch.setattr("baird.scheduler.run_task_once", _recorder)

    task = Task(
        id="cross-cut",
        trigger=IntervalTrigger(interval_seconds=60),
        runnable=Runnable(
            prompt="hi",
            model="anthropic/claude-3-haiku",
            project_ids=["scentinel"],
        ),
        budget=Budget(max_cost_usd=1.0),
    )

    sched = Scheduler(
        hub=hub,
        model_client=_model_client(),
        hub_cfg=HubConfig(daily_total_usd=10.0),
        max_workers=1,
        tick_seconds=0.05,
    )
    sched.set_tasks({"cross-cut": task})
    sched._entries["cross-cut"].next_fire = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    )

    import threading
    th = threading.Thread(target=sched.run, daemon=True)
    th.start()
    time.sleep(0.4)
    sched.stop()
    th.join(timeout=2.0)

    # One firing per child; parent itself replaced.
    assert sorted(fired_pids) == ["scentinel-scrna", "scentinel-spatial"]


def test_single_project_task_unchanged_when_project_ids_empty(
    client: TestClient, monkeypatch
) -> None:
    """Regression guard: tasks that don't use project_ids keep firing
    exactly once with their existing runnable.project_id (no expansion)."""
    hub = _Hub(client)
    client.post("/projects", json={"id": "p", "name": "p"})

    fired: list[str] = []

    def _recorder(task, **kwargs):
        fired.append(task.runnable.project_id or "")
        return None

    monkeypatch.setattr("baird.scheduler.run_task_once", _recorder)

    task = Task(
        id="single",
        trigger=IntervalTrigger(interval_seconds=60),
        runnable=Runnable(prompt="hi", project_id="p"),
        budget=Budget(max_cost_usd=1.0),
    )
    sched = Scheduler(
        hub=hub, model_client=_model_client(),
        hub_cfg=HubConfig(daily_total_usd=10.0),
        tick_seconds=0.05,
    )
    sched.set_tasks({"single": task})
    sched._entries["single"].next_fire = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    )
    import threading
    th = threading.Thread(target=sched.run, daemon=True)
    th.start()
    time.sleep(0.3)
    sched.stop()
    th.join(timeout=2.0)

    assert fired == ["p"]


