"""Tests for the three-tier safe/destructive classifier."""

from __future__ import annotations

from pathlib import Path

from baird.permissions import (
    PolicyOverride,
    Tier,
    classify_command,
    classify_write,
    overrides_from_project_yaml,
)


# ---- Command classification --------------------------------------------


def test_ls_is_safe() -> None:
    d = classify_command("ls -la")
    assert d.tier == Tier.SAFE


def test_git_status_is_safe() -> None:
    assert classify_command("git status").tier == Tier.SAFE
    assert classify_command("git log --oneline").tier == Tier.SAFE
    assert classify_command("git diff HEAD~1").tier == Tier.SAFE


def test_pytest_is_project() -> None:
    assert classify_command("pytest -q").tier == Tier.PROJECT


def test_rm_is_destructive() -> None:
    assert classify_command("rm file.txt").tier == Tier.DESTRUCTIVE


def test_rm_rf_root_is_always_destructive() -> None:
    d = classify_command("rm -rf /")
    assert d.tier == Tier.DESTRUCTIVE
    assert "always-destructive" in d.reason


def test_pip_install_always_destructive_even_with_override() -> None:
    overrides = [PolicyOverride(command_regex=r"^pip install", tier=Tier.PROJECT)]
    d = classify_command("pip install pandas", project_overrides=overrides)
    assert d.tier == Tier.DESTRUCTIVE


def test_sudo_always_destructive() -> None:
    assert classify_command("sudo pytest").tier == Tier.DESTRUCTIVE


def test_unknown_command_defaults_destructive() -> None:
    assert classify_command("weird_binary --foo").tier == Tier.DESTRUCTIVE


def test_project_override_promotes() -> None:
    overrides = [PolicyOverride(command_regex=r"^./run_pipeline\.sh", tier=Tier.PROJECT)]
    d = classify_command("./run_pipeline.sh --input x", project_overrides=overrides)
    assert d.tier == Tier.PROJECT


def test_force_push_always_destructive() -> None:
    assert classify_command("git push --force origin main").tier == Tier.DESTRUCTIVE
    assert classify_command("git push -f").tier == Tier.DESTRUCTIVE


# ---- Path scoping ------------------------------------------------------


def test_write_inside_root_is_project(tmp_path: Path) -> None:
    target = tmp_path / "src" / "x.py"
    d = classify_write(target, project_root=tmp_path)
    assert d.tier == Tier.PROJECT


def test_write_outside_root_is_destructive(tmp_path: Path) -> None:
    d = classify_write(Path("/etc/hostname"), project_root=tmp_path)
    assert d.tier == Tier.DESTRUCTIVE


def test_write_without_project_is_destructive(tmp_path: Path) -> None:
    d = classify_write(tmp_path / "x.txt", project_root=None)
    assert d.tier == Tier.DESTRUCTIVE


# ---- Project YAML extraction -------------------------------------------


def test_overrides_from_project_yaml() -> None:
    d = {
        "permissions": [
            {"command_regex": "^./run\\.sh", "tier": "project", "reason": "vetted runner"},
            {"command_regex": "^bad_form"},  # missing tier — skipped
        ]
    }
    out = overrides_from_project_yaml(d)
    assert len(out) == 1
    assert out[0].tier == Tier.PROJECT
    assert out[0].reason == "vetted runner"
