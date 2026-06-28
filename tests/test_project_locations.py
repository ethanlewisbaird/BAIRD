"""Tests for the multi-location project model (Slice A).

Schema: `Location {host, path, role?}` on `ProjectYaml`, plus `effective_locations()`
which falls back to `checkout_hosts` for legacy rows.

Hub: GET/POST/DELETE on /projects/{id}/locations, JSON-stored in Project.config.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from baird.project_yaml import (
    CheckoutHost,
    Location,
    ProjectYaml,
    effective_locations,
    load_project_yaml,
    save_project_yaml,
)

# ---- Schema -----------------------------------------------------------

def test_locations_round_trip(tmp_path: Path) -> None:
    py = ProjectYaml(
        id="p",
        name="P",
        locations=[
            Location(host="workstation", path="/data/x", role="data"),
            Location(host="gpu", path="/scratch/x", role="compute"),
        ],
    )
    path = tmp_path / "project.yaml"
    save_project_yaml(py, path)
    loaded = load_project_yaml(path)
    assert [loc.host for loc in loaded.locations] == ["workstation", "gpu"]
    assert loaded.locations[0].role == "data"


def test_effective_locations_uses_new_field() -> None:
    py = ProjectYaml(
        id="p",
        name="P",
        locations=[Location(host="workstation", path="/x")],
        checkout_hosts=[CheckoutHost(host_id="laptop", path="/y")],
    )
    locs = effective_locations(py)
    assert len(locs) == 1 and locs[0].host == "workstation"


def test_effective_locations_falls_back_to_checkout_hosts() -> None:
    py = ProjectYaml(
        id="p",
        name="P",
        checkout_hosts=[CheckoutHost(host_id="laptop", path="/y", branch="main")],
    )
    locs = effective_locations(py)
    assert len(locs) == 1
    assert locs[0].host == "laptop"
    assert locs[0].path == "/y"
    assert locs[0].role == "repo"


# ---- Hub endpoints ----------------------------------------------------

def test_list_locations_empty(client: TestClient) -> None:
    client.post("/projects", json={"id": "p", "name": "p"})
    r = client.get("/projects/p/locations")
    assert r.status_code == 200
    assert r.json() == []


def test_add_remove_locations(client: TestClient) -> None:
    client.post("/projects", json={"id": "p", "name": "p"})

    r = client.post(
        "/projects/p/locations",
        json={"host": "workstation", "path": "/data", "role": "data"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1 and body[0]["host"] == "workstation"

    client.post(
        "/projects/p/locations",
        json={"host": "gpu", "path": "/scratch", "role": "compute"},
    )
    assert len(client.get("/projects/p/locations").json()) == 2

    # Adding the same (host, path) again replaces, doesn't duplicate.
    r3 = client.post(
        "/projects/p/locations",
        json={"host": "workstation", "path": "/data", "role": "primary-data"},
    )
    body3 = r3.json()
    assert len(body3) == 2
    workstation = next(loc for loc in body3 if loc["host"] == "workstation")
    assert workstation["role"] == "primary-data"

    # Delete one.
    r4 = client.request(
        "DELETE",
        "/projects/p/locations",
        params={"host": "gpu", "path": "/scratch"},
    )
    assert [loc["host"] for loc in r4.json()] == ["workstation"]


def test_locations_404_for_missing_project(client: TestClient) -> None:
    assert client.get("/projects/ghost/locations").status_code == 404
    r = client.post(
        "/projects/ghost/locations", json={"host": "h", "path": "/p"}
    )
    assert r.status_code == 404


def test_legacy_checkout_hosts_visible_via_locations(client: TestClient) -> None:
    """Legacy project rows that pre-date the locations field should still expose
    their checkout_hosts via GET /locations (auto-migration on read)."""
    client.post(
        "/projects",
        json={
            "id": "legacy",
            "name": "legacy",
            "config": {
                "checkout_hosts": [
                    {"host_id": "laptop", "path": "/home/user/proj", "branch": "main"},
                ]
            },
        },
    )
    rows = client.get("/projects/legacy/locations").json()
    assert len(rows) == 1
    assert rows[0]["host"] == "laptop"
    assert rows[0]["path"] == "/home/user/proj"
    assert rows[0]["role"] == "repo"
