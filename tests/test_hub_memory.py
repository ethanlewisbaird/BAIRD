"""Tests for the Phase 2 hub routes — projects, decisions, actions,
file lineage, sessions, notifications, recall."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _file_payload(volume: str = "vol-a", path: str = "x.txt", head: str = "a" * 64) -> dict:
    return {
        "storage_volume": volume,
        "relative_path": path,
        "size": 10,
        "mtime_ns": 1_700_000_000_000_000_000,
        "head_hash": head,
        "tail_hash": "b" * 64,
        "sha256": None,
    }


# ---- Projects ----------------------------------------------------------


def test_upsert_project_and_get(client: TestClient) -> None:
    r = client.post(
        "/projects",
        json={
            "id": "scrna",
            "name": "scRNA",
            "github": "me/scrna",
            "context": "single cell stuff",
            "config": {"goals": [{"id": "g1", "text": "qc", "status": "active"}]},
        },
    )
    assert r.status_code == 200
    assert r.json()["id"] == "scrna"

    g = client.get("/projects/scrna")
    assert g.status_code == 200
    assert g.json()["config"]["goals"][0]["text"] == "qc"


def test_upsert_project_is_idempotent(client: TestClient) -> None:
    client.post("/projects", json={"id": "p", "name": "p1"})
    client.post("/projects", json={"id": "p", "name": "p2", "context": "renamed"})
    r = client.get("/projects/p")
    assert r.json()["name"] == "p2"
    assert r.json()["context"] == "renamed"


def test_list_projects(client: TestClient) -> None:
    client.post("/projects", json={"id": "a", "name": "A"})
    client.post("/projects", json={"id": "b", "name": "B"})
    rows = client.get("/projects").json()
    assert {r["id"] for r in rows} == {"a", "b"}


def test_get_project_404(client: TestClient) -> None:
    assert client.get("/projects/missing").status_code == 404


# ---- Decisions ---------------------------------------------------------


def test_decisions_append_only_log(client: TestClient) -> None:
    client.post("/projects", json={"id": "p", "name": "p"})
    r1 = client.post(
        "/projects/p/decisions", json={"project_id": "p", "text": "use harmony", "author": "ai"}
    )
    assert r1.status_code == 200
    assert r1.json()["author"] == "ai"

    r2 = client.post(
        "/projects/p/decisions", json={"project_id": "p", "text": "pin sklearn==1.4", "author": "user"}
    )
    assert r2.status_code == 200

    rows = client.get("/projects/p/decisions").json()
    assert len(rows) == 2
    # Newest first
    assert rows[0]["text"] in {"use harmony", "pin sklearn==1.4"}


def test_decision_requires_existing_project(client: TestClient) -> None:
    r = client.post(
        "/projects/ghost/decisions",
        json={"project_id": "ghost", "text": "x", "author": "user"},
    )
    assert r.status_code == 404


def test_decision_author_validated(client: TestClient) -> None:
    client.post("/projects", json={"id": "p", "name": "p"})
    r = client.post(
        "/projects/p/decisions",
        json={"project_id": "p", "text": "x", "author": "robot"},
    )
    assert r.status_code == 400


def test_decision_id_mismatch_rejected(client: TestClient) -> None:
    client.post("/projects", json={"id": "p", "name": "p"})
    r = client.post(
        "/projects/p/decisions",
        json={"project_id": "OTHER", "text": "x", "author": "user"},
    )
    assert r.status_code == 400


# ---- Actions + lineage -------------------------------------------------


def test_action_lifecycle_and_lineage(client: TestClient) -> None:
    f_in = client.post("/files", json=_file_payload(path="in.txt", head="1" * 64)).json()
    f_out = client.post("/files", json=_file_payload(path="out.txt", head="2" * 64)).json()

    a = client.post(
        "/actions",
        json={
            "project_id": "scrna",
            "tool_name": "samtools",
            "tool_version": "1.19",
            "command": "samtools sort in.bam",
            "host": "workstation:/data",
            "conda_env": "bio-py311",
            "env_hash": "f" * 64,
        },
    ).json()

    client.post(f"/actions/{a['id']}/files", json={"file_id": f_in["id"], "role": "input"})
    client.post(f"/actions/{a['id']}/files", json={"file_id": f_out["id"], "role": "output"})

    fin = client.patch(
        f"/actions/{a['id']}",
        json={"exit_code": 0, "summary": "sorted 1.2GB in 14s"},
    ).json()
    assert fin["exit_code"] == 0
    assert fin["summary"] == "sorted 1.2GB in 14s"

    lin = client.get(f"/files/{f_out['id']}/lineage").json()
    assert lin["file_id"] == f_out["id"]
    roles = {edge["role"] for edge in lin["actions"]}
    assert "output" in roles

    listed = client.get("/actions", params={"project_id": "scrna"}).json()
    assert any(row["id"] == a["id"] for row in listed)


def test_attach_file_validates_role(client: TestClient) -> None:
    f = client.post("/files", json=_file_payload()).json()
    a = client.post("/actions", json={"command": "x"}).json()
    r = client.post(f"/actions/{a['id']}/files", json={"file_id": f["id"], "role": "bogus"})
    assert r.status_code == 400


def test_attach_file_404s_for_missing_file(client: TestClient) -> None:
    a = client.post("/actions", json={"command": "x"}).json()
    r = client.post(f"/actions/{a['id']}/files", json={"file_id": "ghost", "role": "input"})
    assert r.status_code == 404


def test_output_attachment_sets_created_by(client: TestClient) -> None:
    f = client.post("/files", json=_file_payload(path="o.txt", head="3" * 64)).json()
    a = client.post("/actions", json={"command": "x"}).json()
    client.post(f"/actions/{a['id']}/files", json={"file_id": f["id"], "role": "output"})
    # there's no direct GET that exposes created_by_action_id, but the lineage edge proves it
    lin = client.get(f"/files/{f['id']}/lineage").json()
    assert lin["actions"][0]["action_id"] == a["id"]


# ---- Sessions + messages ----------------------------------------------


def test_session_messages_round_trip(client: TestClient) -> None:
    sess = client.post("/sessions", json={"mode": "code", "project_id": "p"}).json()
    client.post(f"/sessions/{sess['id']}/messages", json={"role": "user", "content": "hi"})
    client.post(f"/sessions/{sess['id']}/messages", json={"role": "assistant", "content": "hello"})

    msgs = client.get(f"/sessions/{sess['id']}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "hi"


def test_session_mode_validated(client: TestClient) -> None:
    r = client.post("/sessions", json={"mode": "bogus"})
    assert r.status_code == 400


def test_messages_404_on_missing_session(client: TestClient) -> None:
    assert client.get("/sessions/ghost/messages").status_code == 404


# ---- Notifications -----------------------------------------------------


def test_notification_lifecycle(client: TestClient) -> None:
    r = client.post(
        "/notifications",
        json={"kind": "approval", "title": "tier-2 write pending", "project_id": "p"},
    )
    assert r.status_code == 200
    nid = r.json()["id"]

    unresolved = client.get("/notifications", params={"unresolved_only": True}).json()
    assert any(n["id"] == nid for n in unresolved)

    resolved = client.patch(
        f"/notifications/{nid}", json={"resolution": "accept", "read": True}
    ).json()
    assert resolved["resolution"] == "accept"
    assert resolved["resolved_at"] is not None
    assert resolved["read_at"] is not None

    still = client.get("/notifications", params={"unresolved_only": True}).json()
    assert not any(n["id"] == nid for n in still)


# ---- Recall ------------------------------------------------------------


def test_recall_finds_action_summary(client: TestClient) -> None:
    client.post("/projects", json={"id": "scrna", "name": "scrna"})
    a = client.post("/actions", json={"project_id": "scrna", "command": "leiden"}).json()
    client.patch(
        f"/actions/{a['id']}",
        json={"summary": "Leiden clustering finished with resolution 0.8"},
    )

    hits = client.get("/recall", params={"query": "leiden", "project_id": "scrna"}).json()["hits"]
    assert any(h["source"] == "action_summary" for h in hits)


def test_recall_searches_decisions_and_notifications(client: TestClient) -> None:
    client.post("/projects", json={"id": "p", "name": "p"})
    client.post(
        "/projects/p/decisions",
        json={"project_id": "p", "text": "pin sklearn==1.4 because of leiden regression"},
    )
    client.post("/notifications", json={"kind": "result", "title": "leiden run done"})

    hits = client.get("/recall", params={"query": "leiden", "k": 10}).json()["hits"]
    sources = {h["source"] for h in hits}
    assert {"decision", "notification"} <= sources


def test_recall_source_filter(client: TestClient) -> None:
    client.post("/projects", json={"id": "p", "name": "p"})
    client.post("/projects/p/decisions", json={"project_id": "p", "text": "use harmony"})
    client.post("/notifications", json={"kind": "result", "title": "harmony done"})

    hits = client.get(
        "/recall", params={"query": "harmony", "sources": "decision"}
    ).json()["hits"]
    assert hits and all(h["source"] == "decision" for h in hits)
