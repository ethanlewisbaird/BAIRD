"""CLI smoke for Phase 4b/5b: baird session list, baird code REPL (with stubs)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from baird import cli as cli_mod
from baird.memory_client import HubClient


class _TCH(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client

    def close(self) -> None:
        pass


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setattr(cli_mod, "_hub_client_from_host", lambda: _TCH(client))


def test_session_list_empty_via_noop_backend(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the noop backend so we don't depend on tmux/screen on the test box.
    monkeypatch.setattr(shutil, "which", lambda _: None)
    r = runner.invoke(cli_mod.app, ["session", "list"])
    assert r.exit_code == 0
    assert "no none sessions" in r.output


def test_session_attach_prints_command(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    r = runner.invoke(cli_mod.app, ["session", "attach", "x"])
    assert r.exit_code == 0
    # Noop backend's attach_cmd is `true`.
    assert "true" in r.output


def test_status_one_shot_still_works(
    runner: CliRunner, patched: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli_mod, "_tasks_dir", lambda: tmp_path / "tasks")
    r = runner.invoke(cli_mod.app, ["status"])
    assert r.exit_code == 0
    assert "hub OK" in r.output


def test_code_uses_repl(
    runner: CliRunner,
    patched: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`baird code` should run the multi-turn REPL. We stub OpenRouterClient and
    feed stdin with '/exit' so the REPL exits immediately."""
    import subprocess

    from baird.project_yaml import project_yaml_template, save_project_yaml

    # Make a minimal project for the context loader.
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=proj, check=True)
    save_project_yaml(project_yaml_template("cli-repl", "X"), proj / ".baird" / "project.yaml")
    (proj / "README.md").write_text("# X\n")
    subprocess.run(["git", "add", "."], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=proj, check=True)
    monkeypatch.chdir(proj)

    # Stub OpenRouterClient — the CLI does `from .model import OpenRouterClient`
    # *inside* the code() function, so we patch the source module.
    from baird import model as model_mod

    def fake_transport(_req):
        return {
            "choices": [{"message": {"content": "stub reply"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }

    real_cls = model_mod.OpenRouterClient

    def factory(*, transport=None):
        return real_cls(transport=transport or fake_transport)

    monkeypatch.setattr(model_mod, "OpenRouterClient", factory)

    r = runner.invoke(cli_mod.app, ["code", "--no-tui"], input="hello\n/exit\n")
    assert r.exit_code == 0, r.output
    assert "stub reply" in r.output
