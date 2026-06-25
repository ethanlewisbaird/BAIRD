"""Tests for the task schema + loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from baird.tasks import (
    Budget,
    CronTrigger,
    IntervalTrigger,
    Runnable,
    Task,
    load_task,
    load_tasks_dir,
    save_task,
    task_yaml_template,
)


def test_template_round_trip(tmp_path: Path) -> None:
    t = task_yaml_template("daily-poke")
    save_task(t, tmp_path / "daily-poke.yaml")
    loaded = load_task(tmp_path / "daily-poke.yaml")
    assert loaded.id == "daily-poke"
    assert isinstance(loaded.trigger, CronTrigger)
    assert loaded.budget.max_cost_usd == pytest.approx(0.10)


def test_interval_trigger_round_trip(tmp_path: Path) -> None:
    t = Task(
        id="poll",
        trigger=IntervalTrigger(interval_seconds=300),
        runnable=Runnable(prompt="hi"),
    )
    save_task(t, tmp_path / "poll.yaml")
    loaded = load_task(tmp_path / "poll.yaml")
    assert isinstance(loaded.trigger, IntervalTrigger)
    assert loaded.trigger.interval_seconds == 300


def test_load_tasks_dir_skips_bad_files(tmp_path: Path) -> None:
    save_task(task_yaml_template("good"), tmp_path / "good.yaml")
    (tmp_path / "bad.yaml").write_text("this is: not [valid")
    out = load_tasks_dir(tmp_path)
    assert set(out) == {"good"}


def test_load_tasks_dir_empty_when_missing(tmp_path: Path) -> None:
    assert load_tasks_dir(tmp_path / "does-not-exist") == {}


def test_budget_defaults() -> None:
    b = Budget()
    assert b.max_cost_usd is None
    assert b.max_runtime_s is None
