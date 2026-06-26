"""Hub bearer-token authentication."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baird.config import HubConfig
from baird.hub import create_app


@pytest.fixture
def authed_client(tmp_path: Path) -> TestClient:
    cfg = HubConfig(
        registry_db=str(tmp_path / "r.sqlite"),
        memory_db=str(tmp_path / "m.sqlite"),
        auth_token="s3cret",
    )
    return TestClient(create_app(cfg))


def test_health_open_even_with_auth(authed_client: TestClient) -> None:
    r = authed_client.get("/health")
    assert r.status_code == 200


def test_missing_bearer_returns_401(authed_client: TestClient) -> None:
    r = authed_client.get("/projects")
    assert r.status_code == 401


def test_wrong_bearer_returns_401(authed_client: TestClient) -> None:
    r = authed_client.get("/projects", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_correct_bearer_works(authed_client: TestClient) -> None:
    r = authed_client.get("/projects", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200


def test_no_auth_token_means_open(tmp_path: Path) -> None:
    cfg = HubConfig(
        registry_db=str(tmp_path / "r.sqlite"),
        memory_db=str(tmp_path / "m.sqlite"),
    )
    c = TestClient(create_app(cfg))
    assert c.get("/projects").status_code == 200
