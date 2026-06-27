"""Tests for the project enrichment pipeline (Issue 5).

Three layers:
- Unit: `probe_location` over a stubbed RemoteReader returning canned
  file contents.
- Unit: `propose_enrichment` against fabricated probe results.
- Integration: `/project enrich` end-to-end with a fake hub + fake
  remote reader, asserting hub.upsert_project receives the accepted
  values.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from baird.agent_tools import ToolEnv
from baird.project_enrich import (
    github_slug_from_origin,
    probe_location,
    propose_enrichment,
)
from baird.slash import SlashContext, try_dispatch

# ---- Probe ------------------------------------------------------------


def _reader(contents: dict[str, str]):
    """Build a RemoteReader that returns `contents[path]` when present,
    None otherwise. Host is ignored for these unit tests."""
    def read(host: str, path: str) -> str | None:
        return contents.get(path)
    return read


def test_probe_reads_readme_and_truncates() -> None:
    long_readme = "\n".join(f"line {i}" for i in range(100))
    reader = _reader({"/proj/README.md": long_readme})
    probe = probe_location(reader, "hibu", "/proj")
    assert probe.readme is not None
    assert probe.readme.count("\n") + 1 <= 40


def test_probe_extracts_git_origin_https() -> None:
    git_cfg = (
        "[core]\n\trepositoryformatversion = 0\n"
        '[remote "origin"]\n\turl = https://github.com/ethan/scrna.git\n'
    )
    reader = _reader({"/p/.git/config": git_cfg})
    probe = probe_location(reader, "h", "/p")
    assert probe.git_origin == "https://github.com/ethan/scrna.git"


def test_probe_extracts_conda_env_name() -> None:
    env_yml = "name: scrna-env\ndependencies:\n  - python=3.11\n"
    reader = _reader({"/p/environment.yml": env_yml})
    probe = probe_location(reader, "h", "/p")
    assert probe.conda_env_name == "scrna-env"


def test_probe_detects_dockerfile_presence() -> None:
    reader = _reader({"/p/Dockerfile": "FROM python:3.11\n"})
    probe = probe_location(reader, "h", "/p")
    assert probe.has_dockerfile is True


def test_probe_missing_files_leave_slots_empty() -> None:
    probe = probe_location(_reader({}), "h", "/empty")
    assert probe.readme is None
    assert probe.git_origin is None
    assert probe.has_dockerfile is False


def test_probe_extracts_pyproject_description() -> None:
    pyproj = '[project]\nname = "scrna"\ndescription = "single-cell pipeline"\n'
    reader = _reader({"/p/pyproject.toml": pyproj})
    probe = probe_location(reader, "h", "/p")
    assert probe.pyproject_description == "single-cell pipeline"


def test_github_slug_extraction() -> None:
    assert github_slug_from_origin("git@github.com:ethan/scrna.git") == "ethan/scrna"
    assert (
        github_slug_from_origin("https://github.com/ethan/scrna") == "ethan/scrna"
    )
    assert github_slug_from_origin("https://gitlab.com/x/y.git") is None
    assert github_slug_from_origin(None) is None


# ---- Propose ----------------------------------------------------------


from baird.project_enrich import LocationProbe  # noqa: E402


def _probe_with(**kw) -> LocationProbe:
    base = dict(host="h", path="/p")
    base.update(kw)
    return LocationProbe(**base)


def test_propose_fills_github_from_git_origin() -> None:
    current = {"id": "p", "name": "p", "github": None, "context": None, "config": {}}
    probes = [_probe_with(git_origin="git@github.com:ethan/scrna.git")]
    prop = propose_enrichment(current, probes).by_field()
    assert prop["github"].value == "ethan/scrna"


def test_propose_skips_already_filled_fields() -> None:
    current = {
        "id": "p", "name": "p", "github": "x/y", "context": "set",
        "config": {"env": {"conda": "x"}},
    }
    probes = [_probe_with(git_origin="git@github.com:other/z.git", conda_env_name="z")]
    assert propose_enrichment(current, probes).proposals == []


def test_propose_env_combines_signals_across_locations() -> None:
    current = {"id": "p", "config": {}}
    probes = [
        _probe_with(host="hibu", path="/d", conda_env_name="scrna-env"),
        _probe_with(host="gpu", path="/c", has_dockerfile=True),
    ]
    prop = propose_enrichment(current, probes).by_field()
    env = prop["env"].value
    assert env == {"conda": "scrna-env", "docker": True}


def test_propose_returns_none_value_when_nothing_found() -> None:
    """The form layer surfaces `value=None` as '(none found — leave
    blank?)' so the user sees the probe was attempted."""
    current = {"id": "p", "config": {}}
    probes = [_probe_with()]  # nothing
    prop = propose_enrichment(current, probes).by_field()
    assert prop["github"].value is None
    assert prop["context"].value is None
    assert prop["env"].value is None


# ---- Slash-command integration ---------------------------------------


class _FakeExecutor:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self): return self
    def __exit__(self, *a: Any): pass

    def read_file(self, path: str) -> dict:
        self.calls.append(("read_file", {"path": path}))
        if path not in self.files:
            raise FileNotFoundError(path)
        return {"path": path, "content": self.files[path], "size": len(self.files[path])}


@pytest.fixture(autouse=True)
def _stub_satellite_registry(monkeypatch):
    monkeypatch.setattr(
        "baird.satellite.load_registry",
        lambda: {"hibu": {"local_fwd_port": 0, "executor_auth_token": "x"}},
    )


def _slash_ctx(files: dict[str, str], answers: list[str]):
    hub = MagicMock()
    ex = _FakeExecutor(files)
    env = ToolEnv(
        hub=hub,
        executors={"hibu": ("u", "t")},
        executor_factory=lambda *_: ex,
    )
    it = iter(answers)
    return (
        SlashContext(
            hub=hub, env=env, input_fn=lambda _p: next(it),
            console=None, active_host=None,
        ),
        hub,
    )


def test_project_enrich_proposes_and_saves(monkeypatch) -> None:
    files = {
        "/proj/README.md": "scRNA-seq pipeline\n\nDetails here.",
        "/proj/.git/config": '[remote "origin"]\n\turl = git@github.com:ethan/scrna.git\n',
        "/proj/environment.yml": "name: scrna-env\n",
    }
    # Answers: accept all three proposals by hitting "enter" — but the
    # form's input_fn is only invoked for required fields; optional
    # fields with defaults take their default without prompting. So no
    # answers needed.
    ctx, hub = _slash_ctx(files, answers=[])
    hub.get_project.return_value = {
        "id": "scrna", "name": "scRNA", "github": None,
        "context": None, "config": {},
    }
    hub.list_project_locations.return_value = [
        {"host": "hibu", "path": "/proj", "role": None},
    ]
    r = try_dispatch("project enrich scrna", ctx)
    assert r.handled and r.ok, r.output
    # upsert_project was called with the proposed values.
    hub.upsert_project.assert_called_once()
    kw = hub.upsert_project.call_args.kwargs
    assert kw["github"] == "ethan/scrna"
    assert "scRNA-seq pipeline" in (kw["context"] or "")
    assert kw["config"]["env"]["conda"] == "scrna-env"


def test_project_enrich_skips_when_no_locations() -> None:
    ctx, hub = _slash_ctx({}, answers=[])
    hub.get_project.return_value = {"id": "scrna", "name": "scRNA", "config": {}}
    hub.list_project_locations.return_value = []
    r = try_dispatch("project enrich scrna", ctx)
    assert r.handled and r.ok
    assert "no locations" in r.output
    hub.upsert_project.assert_not_called()


def test_project_new_auto_runs_enrichment(monkeypatch) -> None:
    files = {
        "/proj/.git/config": '[remote "origin"]\n\turl = git@github.com:ethan/scrna.git\n',
    }
    ctx, hub = _slash_ctx(files, answers=[])
    # upsert_project is called once for create and a second time by
    # enrichment; control both return values via side_effect.
    hub.upsert_project.side_effect = [
        {"id": "scrna"},  # initial create
        {"id": "scrna"},  # enrichment save
    ]
    hub.add_project_location.return_value = [
        {"host": "hibu", "path": "/proj", "role": None}
    ]
    hub.get_project.return_value = {
        "id": "scrna", "name": "scrna", "github": None,
        "context": None, "config": {},
    }
    hub.list_project_locations.return_value = [
        {"host": "hibu", "path": "/proj", "role": None},
    ]
    r = try_dispatch("project new scrna locations=hibu:/proj", ctx)
    assert r.handled and r.ok, r.output
    # Two upsert_project calls: create + enrichment save.
    assert hub.upsert_project.call_count == 2
    enrichment_kwargs = hub.upsert_project.call_args_list[1].kwargs
    assert enrichment_kwargs["github"] == "ethan/scrna"
    assert "enriched" in r.output
