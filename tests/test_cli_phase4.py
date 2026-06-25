"""Smoke tests for the Phase 4 CLI commands: task add/list/run."""

from __future__ import annotations

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


def test_task_add_writes_yaml(runner: CliRunner, patched: Path) -> None:
    r = runner.invoke(cli_mod.app, ["task", "add", "demo"])
    assert r.exit_code == 0
    assert (patched / "demo.yaml").exists()


def test_task_add_refuses_overwrite(runner: CliRunner, patched: Path) -> None:
    runner.invoke(cli_mod.app, ["task", "add", "demo"])
    r = runner.invoke(cli_mod.app, ["task", "add", "demo"])
    assert r.exit_code == 1


def test_task_list_shows_added(runner: CliRunner, patched: Path) -> None:
    runner.invoke(cli_mod.app, ["task", "add", "alpha"])
    r = runner.invoke(cli_mod.app, ["task", "list"])
    assert r.exit_code == 0
    assert "alpha" in r.output


def test_task_run_unknown_errors(runner: CliRunner, patched: Path) -> None:
    r = runner.invoke(cli_mod.app, ["task", "run", "missing"])
    assert r.exit_code == 1


def test_task_run_with_fake_model(
    runner: CliRunner, patched: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(cli_mod.app, ["task", "add", "demo"])

    # Replace OpenRouterClient with a transport-injected fake at the runner call site.
    from baird import runner as runner_mod
    from baird.model import OpenRouterClient

    def fake_transport(_req: dict) -> dict:
        return {
            "choices": [{"message": {"content": "fake reply"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0001},
        }

    monkeypatch.setattr(
        "baird.cli.OpenRouterClient",
        lambda: OpenRouterClient(transport=fake_transport),
        raising=False,
    )
    # The CLI imports OpenRouterClient lazily inside task_run — patch the source module instead.
    from baird import model as model_mod

    monkeypatch.setattr(
        model_mod, "OpenRouterClient", lambda **k: OpenRouterClient(transport=fake_transport, **k)
    )

    r = runner.invoke(cli_mod.app, ["task", "run", "demo"])
    assert r.exit_code == 0, r.output
    assert "fired" in r.output
    assert "fake reply" in r.output
