"""BAIRD_HOME env-var override."""

from __future__ import annotations

from pathlib import Path

import pytest

from baird import paths
from baird.config import HubConfig


def test_default_is_dot_baird(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAIRD_HOME", raising=False)
    assert paths.baird_home() == Path("~/.baird").expanduser()


def test_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path / "alt"))
    assert paths.baird_home() == tmp_path / "alt"
    assert paths.host_yaml_path() == tmp_path / "alt" / "host.yaml"
    assert paths.hub_config_path() == tmp_path / "alt" / "config.yaml"
    assert paths.tasks_dir() == tmp_path / "alt" / "tasks"
    assert paths.registry_db_path() == tmp_path / "alt" / "registry.sqlite"
    assert paths.memory_db_path() == tmp_path / "alt" / "memory.sqlite"


def test_hub_config_defaults_follow_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    cfg = HubConfig()
    assert cfg.registry_db == str(tmp_path / "registry.sqlite")
    assert cfg.memory_db == str(tmp_path / "memory.sqlite")
