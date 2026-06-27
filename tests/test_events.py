"""Cross-process reactive-event flow."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baird.config import HubConfig
from baird.hub import create_app
from baird.memory_client import HubClient


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


def _hub(tmp_path: Path) -> TestClient:
    cfg = HubConfig(
        registry_db=str(tmp_path / "r.sqlite"),
        memory_db=str(tmp_path / "m.sqlite"),
    )
    return TestClient(create_app(cfg))


def test_emit_then_list_unconsumed(tmp_path: Path) -> None:
    hub = _Hub(_hub(tmp_path))
    e = hub.emit_event("pipeline.done", {"workflow": "x.smk"})
    assert e["name"] == "pipeline.done"
    rows = hub.list_events(unconsumed_only=True)
    assert len(rows) == 1
    assert rows[0]["payload"] == {"workflow": "x.smk"}


def test_consume_marks_event(tmp_path: Path) -> None:
    hub = _Hub(_hub(tmp_path))
    e = hub.emit_event("ping")
    hub.consume_event(e["id"])
    rows = hub.list_events(unconsumed_only=True)
    assert rows == []
    all_rows = hub.list_events(unconsumed_only=False)
    assert len(all_rows) == 1
    assert all_rows[0]["consumed_at"] is not None


def test_consume_missing_returns_404(tmp_path: Path) -> None:
    client = _hub(tmp_path)
    r = client.post("/events/not-a-real-id/consume")
    assert r.status_code == 404
