"""Threaded scheduler — Phase 4 design (#1+#2+#7+#8).

A simple, signal-driven scheduler that runs *inside* the orchestrator process
on the hub. NOT systemd / cron — systemd's only role is to keep this process
alive.

Trigger types supported in this slice:
  - `interval`  : every N seconds
  - `cron`      : standard 5-field crontab via `croniter`

`watch` and `reactive` triggers are accepted by the schema but skipped at
scheduling time — they land in Phase 4b.

Concurrency:
  - Global pool of `max_workers` threads (default 3).
  - Tasks may declare `concurrency_group`; firings within a group serialize
    via a per-group lock.

Budgets are checked just before firing. A budget-blocked firing emits one
`logged`-tier notification per check, so the inbox doesn't blow up.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from croniter import croniter
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .budgets import check_task_budget
from .config import HubConfig
from .event_bus import EventBus, default_bus
from .memory_client import HubClient
from .model import OpenRouterClient
from .notifier import Notifier
from .runner import run_task_once
from .tasks import (
    CronTrigger,
    IntervalTrigger,
    ReactiveTrigger,
    Task,
    WatchTrigger,
)

log = logging.getLogger("baird.scheduler")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def expand_project_ids(hub: HubClient, project_ids: list[str]) -> list[str]:
    """Expand `runnable.project_ids` at fire time.

    When an entry is a parent in the project hierarchy, replace it with its
    children (cross-cutting tasks like "for each assay under SCENTINEL,
    do X"). Leaf ids (no children) pass through unchanged. Order is
    preserved and duplicates collapsed.

    Decision: a parent id is REPLACED, not augmented. Including the parent
    too would fire the umbrella's own conversation on every cross-cutting
    run, which is rarely what these callers want — they can add the parent
    id explicitly if they really do.
    """
    out: list[str] = []
    seen: set[str] = set()
    for pid in project_ids:
        try:
            kids = hub.list_children(pid)
        except Exception:
            kids = []
        if kids:
            for k in kids:
                if k["id"] not in seen:
                    out.append(k["id"])
                    seen.add(k["id"])
        else:
            if pid not in seen:
                out.append(pid)
                seen.add(pid)
    return out


def next_fire_after(task: Task, after: dt.datetime) -> dt.datetime | None:
    """Compute the next scheduled fire time strictly after `after`.

    Returns None for triggers this scheduler doesn't handle yet (watch/reactive)
    or for disabled tasks.
    """
    if not task.enabled:
        return None
    trig = task.trigger
    if isinstance(trig, IntervalTrigger):
        return after + dt.timedelta(seconds=trig.interval_seconds)
    if isinstance(trig, CronTrigger):
        it = croniter(trig.cron, after)
        return it.get_next(dt.datetime)
    return None  # watch / reactive — out of scope for this slice


# ---- Scheduler ---------------------------------------------------------


@dataclass
class _ScheduleEntry:
    task: Task
    next_fire: dt.datetime
    in_flight: Future[Any] | None = None


@dataclass
class Scheduler:
    hub: HubClient
    model_client: OpenRouterClient
    notifier: Notifier | None = None
    hub_cfg: HubConfig = field(default_factory=HubConfig)
    host_id: str | None = None
    max_workers: int = 3
    tick_seconds: float = 1.0
    event_bus: EventBus = field(default_factory=lambda: default_bus)
    # Minimum seconds between firings of the same watch-triggered task, to
    # debounce rapid filesystem chatter (e.g. editor save bursts).
    watch_debounce_s: float = 2.0

    _entries: dict[str, _ScheduleEntry] = field(default_factory=dict)
    _group_locks: dict[str, threading.Lock] = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)
    _pool: ThreadPoolExecutor | None = None
    _observers: list[Observer] = field(default_factory=list)
    _unsubs: list[Callable[[], None]] = field(default_factory=list)
    _last_event_fire: dict[str, dt.datetime] = field(default_factory=dict)
    _event_fire_lock: threading.Lock = field(default_factory=threading.Lock)

    # --- public lifecycle ----------------------------------------------

    def set_tasks(self, tasks: dict[str, Task]) -> None:
        """Replace the schedule wholesale. Existing in-flight firings are not
        cancelled; the next_fire times are recomputed from now. Watch + reactive
        triggers are re-bound: prior subscriptions are dropped and rebuilt."""
        # Tear down prior event/watch wiring.
        for off in self._unsubs:
            off()
        self._unsubs.clear()
        for obs in self._observers:
            obs.stop()
        for obs in self._observers:
            obs.join(timeout=1.0)
        self._observers.clear()

        now = _utcnow()
        new_entries: dict[str, _ScheduleEntry] = {}
        for tid, task in tasks.items():
            trig = task.trigger
            if isinstance(trig, WatchTrigger) and task.enabled:
                self._bind_watch(task, trig)
                continue
            if isinstance(trig, ReactiveTrigger) and task.enabled:
                self._bind_reactive(task, trig)
                continue
            nf = next_fire_after(task, now)
            if nf is None:
                continue
            prior = self._entries.get(tid)
            new_entries[tid] = _ScheduleEntry(
                task=task, next_fire=nf, in_flight=prior.in_flight if prior else None
            )
        self._entries = new_entries

    # --- watch / reactive binding --------------------------------------

    def _bind_watch(self, task: Task, trig: WatchTrigger) -> None:
        path = Path(trig.path).expanduser()
        if not path.exists():
            log.warning("watch trigger for task %s: path %s does not exist; skipping", task.id, path)
            return
        handler = _WatchHandler(self, task, trig)
        obs = Observer()
        obs.schedule(handler, str(path), recursive=True)
        obs.start()
        self._observers.append(obs)
        log.info("watching %s for task %s (events=%s)", path, task.id, trig.events)

    def _bind_reactive(self, task: Task, trig: ReactiveTrigger) -> None:
        def _listener(event: str, payload: dict[str, Any]) -> None:
            self._event_fire(task, source=f"event:{event}")

        off = self.event_bus.subscribe(trig.event, _listener)
        self._unsubs.append(off)
        log.info("task %s subscribed to event '%s'", task.id, trig.event)

    def _event_fire(self, task: Task, *, source: str) -> None:
        """Common path for watch + reactive firings: debounce, budget-check,
        spawn through the pool. Concurrency-group locks still apply."""
        now = _utcnow()
        # Check-and-set under a lock so two concurrent publishes don't both
        # win the debounce race.
        with self._event_fire_lock:
            last = self._last_event_fire.get(task.id)
            if last is not None and (now - last).total_seconds() < self.watch_debounce_s:
                return
            self._last_event_fire[task.id] = now

        check = check_task_budget(hub=self.hub, task=task, hub_cfg=self.hub_cfg)
        if not check.ok:
            log.info("event-fired task %s skipped: %s", task.id, check.reason)
            if self.notifier is not None:
                self.notifier.notify(
                    kind="logged",
                    title=f"task {task.id} skipped ({source})",
                    body=check.reason,
                    task_id=task.id,
                )
            return

        if self._pool is None:
            return  # scheduler not running
        if task.concurrency_group:
            lock = self._group_locks.setdefault(task.concurrency_group, threading.Lock())
            self._pool.submit(self._wrap_with_lock(task, lock))
        else:
            self._pool.submit(lambda: self._do_fire(task))

    def run(self) -> None:
        """Block, ticking until `stop()` is called or SIGINT/SIGTERM arrives."""
        self._pool = ThreadPoolExecutor(max_workers=self.max_workers)
        try:
            while not self._stop.is_set():
                self._tick()
                self._stop.wait(timeout=self.tick_seconds)
        finally:
            for obs in self._observers:
                obs.stop()
            for obs in self._observers:
                obs.join(timeout=1.0)
            self._observers.clear()
            for off in self._unsubs:
                off()
            self._unsubs.clear()
            assert self._pool is not None
            self._pool.shutdown(wait=True)
            self._pool = None

    def stop(self) -> None:
        self._stop.set()

    # --- single tick ---------------------------------------------------

    def _tick(self) -> None:
        now = _utcnow()
        self._poll_hub_events()
        for entry in list(self._entries.values()):
            if entry.in_flight is not None and not entry.in_flight.done():
                continue  # still running from last fire
            if entry.next_fire > now:
                continue
            self._fire(entry, now)

    def _poll_hub_events(self) -> None:
        """Drain unconsumed events from the hub and republish onto the local
        bus so reactive triggers fire. Best-effort: hub down → silent retry."""
        try:
            events = self.hub.list_events(unconsumed_only=True, limit=50)
        except Exception:
            return
        for ev in events:
            try:
                self.event_bus.publish(ev["name"], ev.get("payload") or {})
                self.hub.consume_event(ev["id"])
            except Exception:
                continue

    def _fire(self, entry: _ScheduleEntry, now: dt.datetime) -> None:
        task = entry.task
        check = check_task_budget(hub=self.hub, task=task, hub_cfg=self.hub_cfg)
        if not check.ok:
            log.info("task %s skipped: %s", task.id, check.reason)
            if self.notifier is not None:
                self.notifier.notify(
                    kind="logged",
                    title=f"task {task.id} skipped",
                    body=check.reason,
                    task_id=task.id,
                )
            entry.next_fire = self._reschedule(task, now)
            return

        # If one-shot cron, disable for future ticks.
        if isinstance(task.trigger, CronTrigger) and task.trigger.one_shot:
            task.enabled = False

        # Concurrency group — serialize same-group firings via a lock.
        wrapped: Callable[[], Any]
        if task.concurrency_group:
            lock = self._group_locks.setdefault(task.concurrency_group, threading.Lock())
            wrapped = self._wrap_with_lock(task, lock)
        else:
            wrapped = lambda: self._do_fire(task)

        assert self._pool is not None
        entry.in_flight = self._pool.submit(wrapped)
        entry.next_fire = self._reschedule(task, now)

    def _wrap_with_lock(
        self, task: Task, lock: threading.Lock
    ) -> Callable[[], Any]:
        def _wrapped() -> Any:
            with lock:
                return self._do_fire(task)

        return _wrapped

    def _do_fire(self, task: Task) -> Any:
        # Multi-project tasks: when runnable.project_ids is set, resolve
        # parent ids → children at fire time and run once per resolved id.
        # Empty list (the common case) preserves the single-project firing
        # path with whatever `runnable.project_id` was already configured.
        pids = list(task.runnable.project_ids or [])
        if not pids:
            try:
                return run_task_once(
                    task,
                    hub=self.hub,
                    model_client=self.model_client,
                    notifier=self.notifier,
                    host_id=self.host_id,
                )
            except Exception:
                log.exception("task %s firing raised", task.id)
                return None

        resolved = expand_project_ids(self.hub, pids)
        results: list[Any] = []
        for rpid in resolved:
            sub_task = task.model_copy(deep=True)
            sub_task.runnable.project_id = rpid
            # Drop project_ids on the per-id firing to avoid recursive
            # re-expansion if the runner ever inspects it.
            sub_task.runnable.project_ids = []
            try:
                results.append(
                    run_task_once(
                        sub_task,
                        hub=self.hub,
                        model_client=self.model_client,
                        notifier=self.notifier,
                        host_id=self.host_id,
                    )
                )
            except Exception:
                log.exception("task %s firing for project %s raised", task.id, rpid)
                results.append(None)
        return results

    def _reschedule(self, task: Task, after: dt.datetime) -> dt.datetime:
        nf = next_fire_after(task, after)
        # If the task is disabled or has no more fires, push it far into the
        # future so we don't busy-wake on it. Real cleanup happens on set_tasks().
        return nf or (after + dt.timedelta(days=365))


class _WatchHandler(FileSystemEventHandler):
    """Bridge watchdog events into Scheduler._event_fire."""

    _EVENT_MAP = {
        "created": "on_created",
        "modified": "on_modified",
        "moved": "on_moved",
        "deleted": "on_deleted",
    }

    def __init__(self, scheduler: "Scheduler", task: Task, trig: WatchTrigger) -> None:
        self._sched = scheduler
        self._task = task
        self._wanted = set(trig.events)

    def _dispatch(self, kind: str, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if kind not in self._wanted:
            return
        self._sched._event_fire(self._task, source=f"watch:{kind}:{event.src_path}")

    def on_created(self, event: FileSystemEvent) -> None: self._dispatch("created", event)
    def on_modified(self, event: FileSystemEvent) -> None: self._dispatch("modified", event)
    def on_moved(self, event: FileSystemEvent) -> None: self._dispatch("moved", event)
    def on_deleted(self, event: FileSystemEvent) -> None: self._dispatch("deleted", event)
