"""Tests for the env activation prefix builder."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from baird.env import EnvSpec, resolve_env


def test_override_wins(tmp_path: Path) -> None:
    override = EnvSpec(kind="conda", name="myenv")
    spec = resolve_env(project_root=tmp_path, project_env_cfg={"conda": "other"}, override=override)
    assert spec.name == "myenv"


def test_project_cfg_conda(tmp_path: Path) -> None:
    spec = resolve_env(project_root=tmp_path, project_env_cfg={"conda": "bio-py311"})
    assert spec.kind == "conda"
    assert spec.name == "bio-py311"


def test_project_cfg_docker(tmp_path: Path) -> None:
    spec = resolve_env(project_root=tmp_path, project_env_cfg={"docker": "biocontainers/samtools"})
    assert spec.kind == "docker"
    assert spec.image == "biocontainers/samtools"


def test_project_cfg_singularity(tmp_path: Path) -> None:
    spec = resolve_env(
        project_root=tmp_path,
        project_env_cfg={"singularity": "/sif/x.sif", "bind_paths": ["/data"]},
    )
    assert spec.kind == "singularity"
    assert spec.sif == "/sif/x.sif"
    assert "/data" in spec.bind_paths


def test_autodetect_environment_yml(tmp_path: Path) -> None:
    (tmp_path / "environment.yml").write_text("name: my-detected\ndependencies: []\n")
    spec = resolve_env(project_root=tmp_path)
    assert spec.kind == "conda"
    assert spec.name == "my-detected"


def test_autodetect_dockerfile(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.11\n")
    spec = resolve_env(project_root=tmp_path)
    assert spec.kind == "docker"


def test_autodetect_sif(tmp_path: Path) -> None:
    sif = tmp_path / "tool.sif"
    sif.write_text("not really a sif")
    spec = resolve_env(project_root=tmp_path)
    assert spec.kind == "singularity"
    assert spec.sif == str(sif)


def test_bare_when_nothing(tmp_path: Path) -> None:
    spec = resolve_env(project_root=tmp_path)
    assert spec.kind == "bare"
    assert spec.bare_warning is True


def test_render_prefix_conda(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the tool selection by stubbing shutil.which.
    monkeypatch.setattr("baird.env.shutil.which", lambda name: None if name == "mamba" else "/usr/bin/conda")
    spec = EnvSpec(kind="conda", name="x y")  # shell-quoting test
    prefix = spec.render_prefix()
    assert "conda activate 'x y'" in prefix


def test_render_prefix_docker_mounts_cwd() -> None:
    spec = EnvSpec(kind="docker", image="repo/img:tag")
    prefix = spec.render_prefix(cwd="/work my proj")  # space → forces shlex quoting
    assert "docker run --rm" in prefix
    assert "'/work my proj':/work" in prefix
    assert "repo/img:tag" in prefix


def test_version_descriptor() -> None:
    assert EnvSpec(kind="conda", name="x").version_descriptor() == "conda:x"
    assert EnvSpec(kind="bare").version_descriptor() == "bare"
