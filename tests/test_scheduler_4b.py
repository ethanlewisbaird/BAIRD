"""Phase 4b tests: scheduler watch + reactive triggers."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baird.config import HubConfig
from baird.event_bus import EventBus
from baird.memory_client import HubClient
from baird.model import OpenRouterClient
from baird.scheduler import Scheduler
from baird.tasks import ReactiveTrigger, Runnable, Task, WatchTrigger


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


def _fake_model():
    def t(_req):
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0001},
        }
    return OpenRouterClient(transport=t)


def _sched(client: TestClient, bus: EventBus | None = None) -> Scheduler:
    hub = _Hub(client)
    return Scheduler(
        hub=hub,
        model_client=_fake_model(),
        hub_cfg=HubConfig(daily_total_usd=10.0),
        tick_seconds=0.05,
        event_bus=bus or EventBus(),
        watch_debounce_s=0.05,
    )


def _start(scheduler: Scheduler):
    import threading
    th = threading.Thread(target=scheduler.run, daemon=True)
    th.start()
    return th


# ---- reactive ---------------------------------------------------------


def test_reactive_trigger_fires_on_event(client: TestClient) -> None:
    bus = EventBus()
    sched = _sched(client, bus=bus)
    task = Task(
        id="r1",
        trigger=ReactiveTrigger(event="action.failed_3x"),
        runnable=Runnable(prompt="hi"),
    )
    sched.set_tasks({"r1": task})
    th = _start(sched)
    try:
        time.sleep(0.1)  # let the run-loop spin up so _pool exists
        bus.publish("action.failed_3x", {"task_id": "x"})
        time.sleep(0.3)
    finally:
        sched.stop()
        th.join(timeout=2.0)

    assert _Hub(client).list_actions(task_id="r1"), "expected reactive firing"


def test_reactive_trigger_debounce(client: TestClient) -> None:
    bus = EventBus()
    sched = _sched(client, bus=bus)
    task = Task(id="r2", trigger=ReactiveTrigger(event="ping"), runnable=Runnable(prompt="hi"))
    sched.set_tasks({"r2": task})
    th = _start(sched)
    try:
        time.sleep(0.1)
        for _ in range(5):
            bus.publish("ping", {})
        time.sleep(0.3)
    finally:
        sched.stop()
        th.join(timeout=2.0)

    actions = _Hub(client).list_actions(task_id="r2")
    assert len(actions) == 1, f"debounce should collapse to 1 firing, got {len(actions)}"


# ---- watch ------------------------------------------------------------


def test_watch_trigger_fires_on_file_change(tmp_path: Path, client: TestClient) -> None:
    sched = _sched(client)
    task = Task(
        id="w1",
        trigger=WatchTrigger(path=str(tmp_path), events=["created", "modified"]),
        runnable=Runnable(prompt="hi"),
    )
    sched.set_tasks({"w1": task})
    th = _start(sched)
    try:
        time.sleep(0.1)
        (tmp_path / "trigger.txt").write_text("hello")
        time.sleep(0.4)
    finally:
        sched.stop()
        th.join(timeout=2.0)

    assert _Hub(client).list_actions(task_id="w1"), "expected watch firing"


def test_watch_trigger_missing_path_is_skipped(client: TestClient, tmp_path: Path) -> None:
    sched = _sched(client)
    task = Task(
        id="w-missing",
        trigger=WatchTrigger(path=str(tmp_path / "no-such-dir")),
        runnable=Runnable(prompt="hi"),
    )
    # Should not raise — just log a warning.
    sched.set_tasks({"w-missing": task})
    assert _Hub(client).list_actions(task_id="w-missing") == []


def test_set_tasks_replaces_subscriptions(client: TestClient) -> None:
    bus = EventBus()
    sched = _sched(client, bus=bus)
    sched.set_tasks({
        "a": Task(id="a", trigger=ReactiveTrigger(event="x"), runnable=Runnable(prompt="hi")),
    })
    assert bus.listener_count("x") == 1

    sched.set_tasks({})  # drop everything
    assert bus.listener_count("x") == 0
