"""Tests for the one-level parent/child subproject hierarchy.

Covers:
  - parent_id round-trip through ProjectYaml
  - SubprojectError on illegal hierarchies (self-parent, grandchildren)
  - context_loader emits an inherited Parent section + active parent goals
  - hub /projects?parent_id filter and /projects/{id}/related
  - tasks.resolve_project_ids expands a parent to its children
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from baird.context_loader import ParentSummary, RepoContext, render_context
from baird.project_yaml import (
    ProjectYaml,
    SubprojectError,
    load_project_yaml,
    project_yaml_template,
    save_project_yaml,
    validate_hierarchy,
)
from baird.tasks import Runnable, resolve_project_ids


# ---- ProjectYaml + hierarchy validator ---------------------------------


def test_parent_id_round_trip(tmp_path: Path) -> None:
    py = project_yaml_template("child-a", "Child A", parent_id="parent")
    path = tmp_path / ".baird" / "project.yaml"
    save_project_yaml(py, path)
    loaded = load_project_yaml(path)
    assert loaded.parent_id == "parent"


def test_validate_hierarchy_accepts_one_level() -> None:
    parent = project_yaml_template("parent", "Parent")
    child = project_yaml_template("child-a", "Child A", parent_id="parent")
    validate_hierarchy(child, parent)  # no raise


def test_validate_hierarchy_rejects_self_parent() -> None:
    me = project_yaml_template("p", "p", parent_id="p")
    with pytest.raises(SubprojectError, match="cannot be its own parent"):
        validate_hierarchy(me, me)


def test_validate_hierarchy_rejects_grandchild() -> None:
    grandparent = project_yaml_template("gp", "gp")
    parent = project_yaml_template("p", "p", parent_id="gp")  # parent already has a parent
    child = project_yaml_template("c", "c", parent_id="p")
    with pytest.raises(SubprojectError, match="one level only"):
        validate_hierarchy(child, parent)
    # `grandparent` is only used to confirm the chain is real, not in assertions.
    assert grandparent.id == "gp"


def test_validate_hierarchy_rejects_id_mismatch() -> None:
    parent = project_yaml_template("real-parent", "rp")
    child = project_yaml_template("c", "c", parent_id="someone-else")
    with pytest.raises(SubprojectError, match="parent_id="):
        validate_hierarchy(child, parent)


# ---- context_loader rendering ------------------------------------------


def test_render_context_includes_parent_section() -> None:
    project = ProjectYaml(id="child-a", name="Child A", parent_id="parent")
    parent = ParentSummary(
        id="parent",
        name="Parent",
        context="Umbrella description.",
        active_goals=["goal one", "goal two"],
    )
    ctx = RepoContext(
        project=project,
        project_root=Path("/tmp/x"),
        branch="main",
        parent=parent,
    )
    out = render_context(ctx)
    assert "Parent (Parent)" in out
    assert "inherited" in out
    assert "Umbrella description." in out
    assert "goal one" in out


def test_render_context_omits_parent_section_when_unset() -> None:
    project = ProjectYaml(id="solo", name="Solo")
    ctx = RepoContext(project=project, project_root=Path("/tmp/x"), branch="main")
    out = render_context(ctx)
    assert "Parent (" not in out


# ---- hub endpoints -----------------------------------------------------


def _seed_family(client: TestClient) -> None:
    client.post("/projects", json={
        "id": "parent", "name": "Parent", "context": "umbrella",
        "config": {"goals": [{"id": "g1", "text": "first goal", "status": "active"}]},
    })
    for cid in ("child-a", "child-b"):
        client.post("/projects", json={
            "id": cid, "name": cid, "context": "child",
            "config": {"parent_id": "parent"},
        })
    client.post("/projects", json={
        "id": "unrelated", "name": "unrelated", "context": "lonely", "config": {},
    })


def test_list_projects_filter_by_parent(client: TestClient) -> None:
    _seed_family(client)
    r = client.get("/projects", params={"parent_id": "parent"})
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()}
    assert ids == {"child-a", "child-b"}


def test_related_projects_from_child(client: TestClient) -> None:
    _seed_family(client)
    r = client.get("/projects/child-a/related")
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()}
    # self + parent + sibling, no unrelated
    assert ids == {"child-a", "parent", "child-b"}


def test_related_projects_from_parent(client: TestClient) -> None:
    _seed_family(client)
    r = client.get("/projects/parent/related")
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()}
    assert ids == {"parent", "child-a", "child-b"}


# ---- scheduler/runner fanout resolver ----------------------------------


class _FakeHub:
    def __init__(self, children_map: dict[str, list[dict[str, Any]]]) -> None:
        self._children = children_map

    def list_children(self, parent_id: str) -> list[dict[str, Any]]:
        return self._children.get(parent_id, [])


def test_resolve_project_ids_expands_parent_to_children() -> None:
    hub = _FakeHub({
        "parent": [{"id": "child-a"}, {"id": "child-b"}],
    })
    runnable = Runnable(prompt="x", project_ids=["parent"])
    assert resolve_project_ids(runnable, hub) == ["child-a", "child-b"]


def test_resolve_project_ids_keeps_leaf_as_is() -> None:
    hub = _FakeHub({})
    runnable = Runnable(prompt="x", project_ids=["child-a"])
    assert resolve_project_ids(runnable, hub) == ["child-a"]


def test_resolve_project_ids_falls_back_to_singular() -> None:
    hub = _FakeHub({})
    runnable = Runnable(prompt="x", project_id="single")
    assert resolve_project_ids(runnable, hub) == ["single"]


def test_resolve_project_ids_empty_returns_none_slot() -> None:
    hub = _FakeHub({})
    runnable = Runnable(prompt="x")
    assert resolve_project_ids(runnable, hub) == [None]


def test_resolve_project_ids_dedupes() -> None:
    hub = _FakeHub({
        "parent": [{"id": "a"}, {"id": "b"}],
    })
    runnable = Runnable(prompt="x", project_ids=["parent", "a"])
    out = resolve_project_ids(runnable, hub)
    assert out == ["a", "b"]
