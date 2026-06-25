"""Smoke tests for the project/inbox CLI commands.

The CLI builds its HubClient from ~/.baird/host.yaml. To avoid touching the
user's real config, we monkeypatch `_hub_client_from_host` to return a client
wired to the in-memory TestClient hub.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from baird import cli as cli_mod
from baird.memory_client import HubClient
from baird.project_yaml import load_project_yaml


class _TestClientHub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client

    def close(self) -> None:
        # Each CLI invocation context-manages a fresh HubClient; we don't want
        # those to close the shared TestClient out from under the next call.
        pass


@pytest.fixture
def patched_hub(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> TestClient:
    monkeypatch.setattr(cli_mod, "_hub_client_from_host", lambda: _TestClientHub(client))
    return client


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _run(runner: CliRunner, args: list[str], cwd: Path) -> tuple[int, str]:
    result = runner.invoke(cli_mod.app, args, env={"PWD": str(cwd)})
    return result.exit_code, result.output


def test_project_init_creates_yaml(tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, out = _run(runner, ["project", "init", "my-proj", "--name", "My Proj"], tmp_path)
    assert code == 0
    path = tmp_path / ".baird" / "project.yaml"
    assert path.exists()
    py = load_project_yaml(path)
    assert py.id == "my-proj"
    assert py.name == "My Proj"


def test_project_init_refuses_overwrite_without_force(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _run(runner, ["project", "init", "p"], tmp_path)
    code, _ = _run(runner, ["project", "init", "p"], tmp_path)
    assert code == 1


def test_project_push_then_pull_round_trip(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    patched_hub: TestClient,
) -> None:
    monkeypatch.chdir(tmp_path)
    _run(runner, ["project", "init", "rt", "--name", "Round Trip"], tmp_path)
    code, _ = _run(runner, ["project", "push"], tmp_path)
    assert code == 0

    # The hub now knows about it; pull writes back the same content.
    dest = tmp_path / "elsewhere"
    dest.mkdir()
    monkeypatch.chdir(dest)
    code, _ = _run(runner, ["project", "pull", "rt"], dest)
    assert code == 0
    py = load_project_yaml(dest / ".baird" / "project.yaml")
    assert py.id == "rt"
    assert py.name == "Round Trip"
    # Rules came along through the config dict.
    assert {r.id for r in py.rules} >= {"seeds-set", "readme-present"}


def test_project_list(runner: CliRunner, patched_hub: TestClient) -> None:
    patched_hub.post("/projects", json={"id": "a", "name": "Alpha"})
    code, out = _run(runner, ["project", "list"], Path.cwd())
    assert code == 0
    assert "Alpha" in out


def test_inbox_list_and_resolve(runner: CliRunner, patched_hub: TestClient) -> None:
    n = patched_hub.post(
        "/notifications", json={"kind": "approval", "title": "tier-2 write"}
    ).json()
    code, out = _run(runner, ["inbox"], Path.cwd())
    assert code == 0
    assert n["id"][:8] in out

    code, _ = _run(runner, ["inbox", "resolve", n["id"], "accept"], Path.cwd())
    assert code == 0
    # No longer in unresolved view.
    code, out = _run(runner, ["inbox", "--unresolved"], Path.cwd())
    assert code == 0
    assert n["id"][:8] not in out
