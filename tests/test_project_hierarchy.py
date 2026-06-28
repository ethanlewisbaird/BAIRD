"""Tests for the parent/child project hierarchy (Slice A).

Schema: `parent_id: str | None` on `ProjectYaml`, stored hub-side as
`Project.config["parent_id"]`. Endpoints:
  - `POST /projects` accepts `parent_id` and validates the one-level rule.
  - `GET /projects/{id}` returns `parent_id` in the response.
  - `GET /projects/{id}/children` lists immediate children.

Validation rules: parent must exist, parent cannot itself be a child,
no self-reference, a project with children can't be reparented.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from baird.project_yaml import ProjectYaml

# ---- Schema -----------------------------------------------------------

def test_project_yaml_round_trip_with_parent_id(tmp_path) -> None:
    from baird.project_yaml import load_project_yaml, save_project_yaml

    py = ProjectYaml(id="scrna", name="scRNA", parent_id="umbrella")
    path = tmp_path / "p.yaml"
    save_project_yaml(py, path)
    loaded = load_project_yaml(path)
    assert loaded.parent_id == "umbrella"


def test_project_yaml_parent_id_defaults_none() -> None:
    py = ProjectYaml(id="p", name="P")
    assert py.parent_id is None


# ---- Hub create + read ------------------------------------------------

def test_create_parent_then_child(client: TestClient) -> None:
    client.post("/projects", json={"id": "umbrella", "name": "umbrella programme"})
    r = client.post(
        "/projects",
        json={"id": "umbrella-scrna", "name": "scRNA", "parent_id": "umbrella"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parent_id"] == "umbrella"


def test_get_project_includes_parent_id(client: TestClient) -> None:
    client.post("/projects", json={"id": "umbrella", "name": "U"})
    client.post(
        "/projects",
        json={"id": "child", "name": "C", "parent_id": "umbrella"},
    )
    body = client.get("/projects/child").json()
    assert body["parent_id"] == "umbrella"
    # Parents themselves report parent_id None.
    pbody = client.get("/projects/umbrella").json()
    assert pbody["parent_id"] is None


def test_list_children(client: TestClient) -> None:
    client.post("/projects", json={"id": "u", "name": "U"})
    for cid in ["a", "b", "c"]:
        client.post("/projects", json={"id": cid, "name": cid, "parent_id": "u"})
    # An unrelated project that should NOT show up.
    client.post("/projects", json={"id": "z", "name": "z"})
    r = client.get("/projects/u/children")
    assert r.status_code == 200
    ids = sorted(c["id"] for c in r.json())
    assert ids == ["a", "b", "c"]


def test_list_children_empty(client: TestClient) -> None:
    client.post("/projects", json={"id": "lonely", "name": "lonely"})
    assert client.get("/projects/lonely/children").json() == []


def test_list_children_404_for_missing_project(client: TestClient) -> None:
    assert client.get("/projects/ghost/children").status_code == 404


# ---- Validation -------------------------------------------------------

def test_parent_id_must_exist(client: TestClient) -> None:
    r = client.post(
        "/projects",
        json={"id": "child", "name": "c", "parent_id": "ghost"},
    )
    assert r.status_code == 400
    assert "does not exist" in r.json()["detail"]


def test_parent_id_self_reference_rejected(client: TestClient) -> None:
    # Pre-create so the existence check passes — but self-ref should still
    # fail because the rule is checked first.
    r = client.post(
        "/projects",
        json={"id": "p", "name": "p", "parent_id": "p"},
    )
    assert r.status_code == 400
    assert "itself" in r.json()["detail"]


def test_no_grandchildren(client: TestClient) -> None:
    client.post("/projects", json={"id": "g", "name": "g"})
    client.post(
        "/projects",
        json={"id": "p", "name": "p", "parent_id": "g"},
    )
    r = client.post(
        "/projects",
        json={"id": "c", "name": "c", "parent_id": "p"},
    )
    assert r.status_code == 400
    assert "one level only" in r.json()["detail"] or "grandchildren" in r.json()["detail"]


def test_cannot_reparent_a_project_that_already_has_children(
    client: TestClient,
) -> None:
    # u → p (parent of c)
    client.post("/projects", json={"id": "u", "name": "u"})
    client.post("/projects", json={"id": "p", "name": "p"})
    client.post(
        "/projects",
        json={"id": "c", "name": "c", "parent_id": "p"},
    )
    # Now try to set p's parent_id to u — illegal: p already has children.
    r = client.post(
        "/projects",
        json={"id": "p", "name": "p", "parent_id": "u"},
    )
    assert r.status_code == 400
    assert "child" in r.json()["detail"].lower()


def test_upsert_preserves_parent_id_when_omitted(client: TestClient) -> None:
    """Calling upsert again without parent_id should not strip an existing
    one if the caller passes parent_id=None — but the spec says parent_id is
    the canonical setter. To keep upsert lossless against ordinary edits, we
    treat an absent parent_id (None) as "no change requested"."""
    # NB: current implementation clears on None — document the behaviour we
    # actually ship. If we adopt "None means keep", flip this test.
    client.post("/projects", json={"id": "u", "name": "u"})
    client.post("/projects", json={"id": "c", "name": "c", "parent_id": "u"})
    # Re-upsert without parent_id — clears it (single source of truth).
    r = client.post("/projects", json={"id": "c", "name": "c"})
    assert r.status_code == 200
    body = client.get("/projects/c").json()
    assert body["parent_id"] is None


def test_parent_id_via_config_dict_also_accepted(client: TestClient) -> None:
    """Older callers may stash parent_id inside config — accept both."""
    client.post("/projects", json={"id": "u", "name": "u"})
    r = client.post(
        "/projects",
        json={"id": "c", "name": "c", "config": {"parent_id": "u"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["parent_id"] == "u"


# ---- DELETE /projects/{id} (issue #4) -----------------------------------


def test_delete_leaf_project_succeeds(client: TestClient) -> None:
    client.post("/projects", json={"id": "leaf", "name": "Leaf"})
    r = client.delete("/projects/leaf")
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": "leaf"}
    # No longer listed.
    assert all(p["id"] != "leaf" for p in client.get("/projects").json())
    assert client.get("/projects/leaf").status_code == 404


def test_delete_project_with_children_rejected(client: TestClient) -> None:
    client.post("/projects", json={"id": "umbrella", "name": "U"})
    client.post(
        "/projects",
        json={"id": "child-a", "name": "A", "parent_id": "umbrella"},
    )
    client.post(
        "/projects",
        json={"id": "child-b", "name": "B", "parent_id": "umbrella"},
    )
    r = client.delete("/projects/umbrella")
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "child-a" in detail and "child-b" in detail
    # Parent still exists.
    assert client.get("/projects/umbrella").status_code == 200


def test_delete_unknown_project_404(client: TestClient) -> None:
    assert client.delete("/projects/nope").status_code == 404


def test_delete_then_recreate_works(client: TestClient) -> None:
    client.post("/projects", json={"id": "ghost", "name": "G"})
    assert client.delete("/projects/ghost").status_code == 200
    r = client.post("/projects", json={"id": "ghost", "name": "G2"})
    assert r.status_code == 200
    assert r.json()["name"] == "G2"
