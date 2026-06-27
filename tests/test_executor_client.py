"""ExecutorClient: auto-loads project.yaml permissions and wraps /exec routes."""

from __future__ import annotations

from pathlib import Path

import pytest

from baird.executor_client import ExecutorClient, _load_overrides
from baird.project_yaml import PolicyOverrideSpec, project_yaml_template, save_project_yaml


def test_load_overrides_returns_empty_without_project_yaml(tmp_path: Path) -> None:
    assert _load_overrides(tmp_path) == []
    assert _load_overrides(None) == []


def test_load_overrides_pulls_from_project_yaml(tmp_path: Path) -> None:
    py = project_yaml_template("p1", "P1")
    py.permissions = [
        PolicyOverrideSpec(
            command_regex=r"^\./run\.sh", tier="project", reason="vetted"
        )
    ]
    save_project_yaml(py, tmp_path / ".baird" / "project.yaml")
    out = _load_overrides(tmp_path)
    assert len(out) == 1
    assert out[0]["tier"] == "project"


def test_run_command_packages_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling run_command on a project_root with overrides should serialise
    them into the request body."""
    py = project_yaml_template("p2", "P2")
    py.permissions = [
        PolicyOverrideSpec(command_regex=r"^make ", tier="project", reason="ok")
    ]
    save_project_yaml(py, tmp_path / ".baird" / "project.yaml")

    captured: dict = {}

    class _FakeClient:
        def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return _FakeResp({"exit_code": 0, "stdout": "", "stderr": "", "tier": "project"})

        def close(self):
            pass

        def get(self, url):
            return _FakeResp({"status": "ok"})

    c = ExecutorClient("http://x", "tok")
    c._client = _FakeClient()  # type: ignore[assignment]

    c.run_command("make test", project_root=tmp_path)
    assert captured["url"] == "/exec/run_command"
    assert captured["json"]["command"] == "make test"
    assert captured["json"]["project_root"] == str(tmp_path)
    assert len(captured["json"]["project_overrides"]) == 1
    assert captured["json"]["project_overrides"][0]["tier"] == "project"


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p
