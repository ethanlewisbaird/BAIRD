"""Smoke tests for the Phase 3 CLI commands: `baird code --show-context`,
`baird diff apply`, `baird undo`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from baird import cli as cli_mod
from baird.project_yaml import project_yaml_template, save_project_yaml


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    root = tmp_path / "p"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.test")
    _git(root, "config", "user.name", "tester")
    save_project_yaml(project_yaml_template("p1", "P One"), root / ".baird" / "project.yaml")
    (root / "README.md").write_text("# P One\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_code_show_context(
    runner: CliRunner, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(proj)
    # Make the hub-client construction harmlessly fail so the offline path is used.
    monkeypatch.setattr(cli_mod, "_hub_client_from_host", lambda: (_ for _ in ()).throw(RuntimeError("no hub")))
    result = runner.invoke(cli_mod.app, ["code", "--show-context"])
    assert result.exit_code == 0
    assert "P One" in result.output
    assert "Active rules" in result.output


def test_code_without_project_yaml_errors(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli_mod.app, ["code", "--show-context"])
    assert result.exit_code == 1


def test_diff_apply_then_undo(runner: CliRunner, proj: Path) -> None:
    patch = proj / "edit.patch"
    patch.write_text(
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1 @@\n"
        "-# P One\n"
        "+# P One — updated\n"
    )
    r = runner.invoke(cli_mod.app, ["diff", "apply", str(patch), "-m", "tweak title", "--repo", str(proj)])
    assert r.exit_code == 0, r.output
    assert "# P One — updated" in (proj / "README.md").read_text()

    r = runner.invoke(cli_mod.app, ["undo", "--repo", str(proj)])
    assert r.exit_code == 0, r.output
    assert (proj / "README.md").read_text() == "# P One\n"
