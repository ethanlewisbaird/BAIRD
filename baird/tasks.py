"""Task schema + loader — Phase 4 design (#1+#2).

Tasks live in `<baird_home>/tasks/<id>.yaml`. One file per task, single unified
format with three trigger types:

  - cron:   schedule field is a cron expression
  - interval: simple "every N seconds"
  - watch:  filesystem event (deferred — schema is here, scheduler ignores it)
  - reactive: in-process event bus (deferred — same)

Background tasks run through the same code path as interactive coding —
"background = no human in the loop." This module is purely declarative: load
YAML → typed model. Firing logic is in `scheduler.py` and `runner.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


TASKS_DIR_DEFAULT = "~/.baird/tasks"


# ---- Triggers ----------------------------------------------------------


class CronTrigger(BaseModel):
    type: Literal["cron"] = "cron"
    cron: str  # standard 5-field "m h dom mon dow"
    one_shot: bool = False  # cron tasks that disable themselves after firing


class IntervalTrigger(BaseModel):
    type: Literal["interval"] = "interval"
    interval_seconds: int


class WatchTrigger(BaseModel):
    type: Literal["watch"] = "watch"
    path: str
    events: list[str] = Field(default_factory=lambda: ["created", "modified"])


class ReactiveTrigger(BaseModel):
    type: Literal["reactive"] = "reactive"
    event: str  # e.g. "session.ended", "action.failed_3x"


Trigger = CronTrigger | IntervalTrigger | WatchTrigger | ReactiveTrigger


# ---- Runnable, budget --------------------------------------------------


class Runnable(BaseModel):
    # `kind` picks the executor: a free-form model call vs. one of the
    # built-in agentic loops. For `self_improve` and `research`, `prompt` is
    # unused (the loop has its own prompt) — but the field is required so the
    # YAML stays uniform.
    kind: str = "model"  # model | self_improve | research
    prompt: str = ""
    model: str = "anthropic/claude-3-haiku"
    system: str | None = None
    project_id: str | None = None
    # Multi-project fanout. When set, the task fires once PER resolved project
    # id (each firing gets its own Action + Session). If an entry names a
    # parent project, the runner expands it to that parent's children
    # (per the subproject-hierarchy design). `project_id` is used only when
    # `project_ids` is empty.
    project_ids: list[str] = Field(default_factory=list)
    context_sources: list[str] = Field(default_factory=list)  # e.g. ["repo", "decisions", "rules"]
    max_tokens: int = 1024
    temperature: float = 0.2
    # Where to run. `null` (the default) means "on the hub itself". When set,
    # the orchestrator dispatches the run_command piece to that satellite's
    # executor (see baird.executor_client.ExecutorClient).
    host_id: str | None = None
    # `research` kind: the query to send. `self_improve` kind: max history
    # window to review.
    args: dict[str, Any] = Field(default_factory=dict)


class Budget(BaseModel):
    max_runtime_s: int | None = None
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_actions: int | None = None


# ---- Task --------------------------------------------------------------


class Task(BaseModel):
    id: str
    description: str | None = None
    enabled: bool = True
    trigger: Trigger
    runnable: Runnable
    budget: Budget = Field(default_factory=Budget)
    concurrency_group: str | None = None
    on_failure: dict[str, Any] = Field(default_factory=dict)


# ---- IO helpers --------------------------------------------------------


def load_task(path: Path) -> Task:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return Task(**data)


def save_task(task: Task, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(task.model_dump(mode="json"), f, sort_keys=False)


def load_tasks_dir(directory: Path | None = None) -> dict[str, Task]:
    from . import paths

    d = directory.expanduser() if directory is not None else paths.tasks_dir()
    if not d.exists():
        return {}
    out: dict[str, Task] = {}
    for p in sorted(d.glob("*.yaml")):
        try:
            t = load_task(p)
        except Exception:
            # Bad YAML file shouldn't take the orchestrator down — skip + warn upstream.
            continue
        out[t.id] = t
    return out


def task_yaml_template(task_id: str) -> Task:
    """Starter task — daily 9am cron firing a chat-style prompt."""
    return Task(
        id=task_id,
        description="Edit me — describe what this task does and why.",
        trigger=CronTrigger(cron="0 9 * * *"),
        runnable=Runnable(
            prompt="What should I look at today?",
            model="anthropic/claude-3-haiku",
        ),
        budget=Budget(max_cost_usd=0.10, max_runtime_s=120),
    )


def resolve_project_ids(runnable: "Runnable", hub: Any) -> list[str | None]:
    """Expand `runnable.project_ids` (or fall back to singular `project_id`)
    into the concrete list of project ids to fire against.

    Parent ids expand to their children (one-level hierarchy). Duplicates are
    removed while preserving first-seen order. An empty result means "fire once
    with no project context" (returns `[None]`)."""
    raw = list(runnable.project_ids) if runnable.project_ids else (
        [runnable.project_id] if runnable.project_id else []
    )
    if not raw:
        return [None]

    out: list[str] = []
    seen: set[str] = set()
    for pid in raw:
        if pid is None:
            continue
        children: list[dict] = []
        try:
            children = hub.list_children(pid)
        except Exception:
            children = []
        if children:
            for child in children:
                cid = child["id"]
                if cid not in seen:
                    seen.add(cid)
                    out.append(cid)
        else:
            if pid not in seen:
                seen.add(pid)
                out.append(pid)
    return list(out) if out else [None]


__all__ = [
    "Task",
    "Trigger",
    "CronTrigger",
    "IntervalTrigger",
    "WatchTrigger",
    "ReactiveTrigger",
    "resolve_project_ids",
    "Runnable",
    "Budget",
    "load_task",
    "save_task",
    "load_tasks_dir",
    "task_yaml_template",
    "TASKS_DIR_DEFAULT",
]
