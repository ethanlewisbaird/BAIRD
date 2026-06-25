"""Tests for the /stats endpoint and the new /actions filters."""

from __future__ import annotations

import datetime as dt
import time

from fastapi.testclient import TestClient


def _file_payload(path: str = "x.txt", head: str = "a" * 64) -> dict:
    return {
        "storage_volume": "vol",
        "relative_path": path,
        "size": 1,
        "mtime_ns": 1,
        "head_hash": head,
        "tail_hash": "b" * 64,
        "sha256": None,
    }


def test_stats_empty_hub(client: TestClient) -> None:
    r = client.get("/stats").json()
    assert r == {
        "files_live": 0,
        "actions_total": 0,
        "actions_running": 0,
        "projects": 0,
        "decisions": 0,
        "notifications_unresolved": 0,
    }


def test_stats_counts_propagate(client: TestClient) -> None:
    client.post("/projects", json={"id": "p", "name": "p"})
    client.post("/projects/p/decisions", json={"project_id": "p", "text": "x"})
    client.post("/files", json=_file_payload())
    a = client.post("/actions", json={"command": "x", "project_id": "p"}).json()
    # Leave action unfinished — should count as running.
    client.post("/notifications", json={"kind": "logged", "title": "n1"})

    r = client.get("/stats").json()
    assert r["files_live"] == 1
    assert r["projects"] == 1
    assert r["decisions"] == 1
    assert r["actions_total"] == 1
    assert r["actions_running"] == 1
    assert r["notifications_unresolved"] == 1

    # Finishing it should drop the running count.
    client.patch(f"/actions/{a['id']}", json={"exit_code": 0, "summary": "ok"})
    r2 = client.get("/stats").json()
    assert r2["actions_running"] == 0
    assert r2["actions_total"] == 1


def test_actions_filter_started_after(client: TestClient) -> None:
    a1 = client.post("/actions", json={"command": "old"}).json()
    time.sleep(0.05)
    cutoff = dt.datetime.now(dt.timezone.utc).isoformat()
    time.sleep(0.05)
    a2 = client.post("/actions", json={"command": "new"}).json()

    rows = client.get("/actions", params={"started_after": cutoff}).json()
    ids = {r["id"] for r in rows}
    assert a2["id"] in ids
    assert a1["id"] not in ids


def test_actions_filter_unfinished_only(client: TestClient) -> None:
    a1 = client.post("/actions", json={"command": "x"}).json()
    a2 = client.post("/actions", json={"command": "y"}).json()
    client.patch(f"/actions/{a1['id']}", json={"exit_code": 0})

    rows = client.get("/actions", params={"unfinished_only": True}).json()
    ids = {r["id"] for r in rows}
    assert ids == {a2["id"]}
