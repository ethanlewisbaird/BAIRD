"""Tests for the HubClient — exercising the Phase 2 surface against the real
hub via FastAPI's TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from baird.memory_client import HubClient


class _TestClientHub(HubClient):
    """Bypass the network: the HubClient's underlying httpx.Client is replaced
    by FastAPI's TestClient (itself an httpx.Client subclass)."""

    def __init__(self, client: TestClient) -> None:
        self._client = client


@pytest.fixture
def hub(client: TestClient) -> _TestClientHub:
    return _TestClientHub(client)


def test_project_round_trip(hub: _TestClientHub) -> None:
    hub.upsert_project(id="p", name="P", github="me/p", context="ctx")
    got = hub.get_project("p")
    assert got["name"] == "P"
    listed = hub.list_projects()
    assert any(r["id"] == "p" for r in listed)


def test_decisions(hub: _TestClientHub) -> None:
    hub.upsert_project(id="p", name="P")
    hub.record_decision("p", "use scanpy", author="ai")
    hub.record_decision("p", "pin numpy", author="user")
    rows = hub.list_decisions("p")
    assert len(rows) == 2


def test_start_action_clean_exit(hub: _TestClientHub, client: TestClient) -> None:
    with hub.start_action(command="echo hi", host="laptop:/", project_id="p") as h:
        h.set_summary("ran echo")
    row = client.get(f"/actions/{h.id}").json()
    assert row["exit_code"] == 0
    assert row["summary"] == "ran echo"
    assert row["finished_at"] is not None


def test_start_action_on_exception_marks_exit_1(hub: _TestClientHub, client: TestClient) -> None:
    with pytest.raises(RuntimeError):
        with hub.start_action(command="boom") as h:
            h.set_summary("about to fail")
            raise RuntimeError("nope")
    row = client.get(f"/actions/{h.id}").json()
    assert row["exit_code"] == 1
    assert row["summary"] == "about to fail"


def test_start_action_attach_files_records_lineage(hub: _TestClientHub) -> None:
    f = hub.register_file(
        storage_volume="vol",
        relative_path="o.txt",
        size=1,
        mtime_ns=1,
        head_hash="a" * 64,
        tail_hash="b" * 64,
    )
    with hub.start_action(command="cp") as h:
        h.attach(f["id"], "output")
    lin = hub.file_lineage(f["id"])
    assert lin["actions"][0]["action_id"] == h.id
    assert lin["actions"][0]["role"] == "output"


def test_sessions(hub: _TestClientHub) -> None:
    sess = hub.new_session(mode="chat")
    hub.append_message(sess["id"], role="user", content="hello")
    hub.append_message(
        sess["id"], role="assistant", content="hi", tool_calls=[{"name": "x"}]
    )
    msgs = hub.get_messages(sess["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["tool_calls"] == [{"name": "x"}]


def test_notifications(hub: _TestClientHub) -> None:
    n = hub.create_notification(kind="approval", title="approve this")
    unresolved = hub.list_notifications(unresolved_only=True)
    assert any(r["id"] == n["id"] for r in unresolved)
    r = hub.resolve_notification(n["id"], resolution="accept")
    assert r["resolution"] == "accept"
    assert hub.list_notifications(unresolved_only=True) == []


def test_recall(hub: _TestClientHub) -> None:
    hub.upsert_project(id="p", name="p")
    hub.record_decision("p", "use scanpy 1.10 for integration")
    hits = hub.recall("scanpy", project_id="p")
    assert hits and hits[0]["source"] == "decision"


def test_health(hub: _TestClientHub) -> None:
    assert hub.health() == {"status": "ok"}
