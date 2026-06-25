"""Smoke tests for the Phase 5 CLI: status / logs / ps / registry / mode-hints."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from baird import cli as cli_mod
from baird.memory_client import HubClient


class _TestClientHub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client

    def close(self) -> None:
        pass


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, client: TestClient, tmp_path: Path) -> Path:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.setattr(cli_mod, "_tasks_dir", lambda: tasks_dir)
    monkeypatch.setattr(cli_mod, "_hub_client_from_host", lambda: _TestClientHub(client))
    return tasks_dir


# ---- status -----------------------------------------------------------


def test_status_runs_against_empty_hub(runner: CliRunner, patched: Path) -> None:
    r = runner.invoke(cli_mod.app, ["status"])
    assert r.exit_code == 0, r.output
    assert "hub OK" in r.output
    assert "registry + memory" in r.output
    assert "budget" in r.output


# ---- logs / ps / registry --------------------------------------------


def test_logs_missing_action(runner: CliRunner, patched: Path) -> None:
    r = runner.invoke(cli_mod.app, ["logs", "ghost"])
    assert r.exit_code == 1


def test_logs_existing_action(runner: CliRunner, patched: Path, client: TestClient) -> None:
    a = client.post(
        "/actions",
        json={"command": "echo hi", "project_id": "p", "task_id": "t"},
    ).json()
    client.patch(
        f"/actions/{a['id']}",
        json={"exit_code": 0, "summary": "did it", "cost_usd": 0.0123},
    )
    r = runner.invoke(cli_mod.app, ["logs", a["id"]])
    assert r.exit_code == 0
    assert "did it" in r.output
    assert "echo hi" in r.output


def test_ps_lists_unfinished(runner: CliRunner, patched: Path, client: TestClient) -> None:
    a = client.post("/actions", json={"command": "running"}).json()
    b = client.post("/actions", json={"command": "done"}).json()
    client.patch(f"/actions/{b['id']}", json={"exit_code": 0})

    r = runner.invoke(cli_mod.app, ["ps"])
    assert r.exit_code == 0
    assert a["id"][:8] in r.output
    assert b["id"][:8] not in r.output


def test_registry_actions_filter_by_task(
    runner: CliRunner, patched: Path, client: TestClient
) -> None:
    a_match = client.post("/actions", json={"command": "match", "task_id": "T"}).json()
    a_other = client.post("/actions", json={"command": "nomatch", "task_id": "OTHER"}).json()

    r = runner.invoke(cli_mod.app, ["registry", "actions", "--task", "T"])
    assert r.exit_code == 0
    assert a_match["id"][:8] in r.output
    assert a_other["id"][:8] not in r.output


def test_task_history(runner: CliRunner, patched: Path, client: TestClient) -> None:
    a = client.post("/actions", json={"command": "ping", "task_id": "demo"}).json()
    client.patch(f"/actions/{a['id']}", json={"exit_code": 0, "summary": "pong"})

    r = runner.invoke(cli_mod.app, ["task", "history", "demo"])
    assert r.exit_code == 0
    assert "pong" in r.output


# ---- mode auto-detect hints ------------------------------------------


def test_bare_baird_hints_for_project_dir(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from baird.project_yaml import project_yaml_template, save_project_yaml

    save_project_yaml(project_yaml_template("p1", "P One"), tmp_path / ".baird" / "project.yaml")
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(cli_mod.app, [])
    # exit_code is 0 on Typer help display, but CliRunner reports the SystemExit code
    assert "Detected project" in r.output
    assert "P One" in r.output


def test_bare_baird_hints_for_plain_git_repo(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(cli_mod.app, [])
    assert "Plain git repo" in r.output


def test_bare_baird_hints_for_plain_dir(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(cli_mod.app, [])
    assert "No project here" in r.output


def test_bare_baird_hints_for_notes_dir(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = tmp_path / "papers"
    notes.mkdir()
    monkeypatch.chdir(notes)
    r = runner.invoke(cli_mod.app, [])
    assert "notes/research" in r.output
