"""Shared fixtures.

`hub_app` builds a fresh FastAPI app bound to a tmp_path so tests never touch
the user's real `~/.baird/` SQLite files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baird.config import HubConfig
from baird.hub import create_app


@pytest.fixture
def hub_cfg(tmp_path: Path) -> HubConfig:
    return HubConfig(
        registry_db=str(tmp_path / "registry.sqlite"),
        memory_db=str(tmp_path / "memory.sqlite"),
    )


@pytest.fixture
def client(hub_cfg: HubConfig) -> TestClient:
    app = create_app(hub_cfg)
    return TestClient(app)
