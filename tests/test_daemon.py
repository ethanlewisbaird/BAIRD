"""Tests for the watchdog daemon: path → volume resolution, scope filtering,
and the end-to-end watch loop against a TestClient-backed hub."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baird.config import HostConfig, HubConfig, VolumeSpec, WatchSpec
from baird.daemon import WatchdogDaemon
from baird.hub import create_app
from baird.memory_client import HubClient


class _NoopHub:
    """No-op hub for tests that only exercise daemon internals not touching the network."""

    def register_file(self, **_: object) -> dict:
        return {}

    def list_files(self, **_: object) -> list:
        return []

    def patch_file(self, *_: object, **__: object) -> dict:
        return {}

    def close(self) -> None:
        pass


class _TestClientHub(HubClient):
    """HubClient whose httpx.Client is replaced by FastAPI's TestClient — exercises real route handlers."""

    def __init__(self, client: TestClient) -> None:
        self._client = client  # TestClient is itself an httpx.Client


def _make_host_cfg(mount: Path) -> HostConfig:
    return HostConfig(
        host_id="testhost",
        hub_url="http://test",
        session_multiplexer="none",
        volumes=[VolumeSpec(id="testhost:/work", mount=str(mount), shared=False)],
        watch=WatchSpec(
            roots=[str(mount)],
            deny=["**/.git/**", "**/*.swp", "**/__pycache__/**"],
        ),
    )


@pytest.fixture
def real_client(tmp_path: Path) -> TestClient:
    cfg = HubConfig(
        registry_db=str(tmp_path / "registry.sqlite"),
        memory_db=str(tmp_path / "memory.sqlite"),
    )
    return TestClient(create_app(cfg))


@pytest.fixture
def watch_dir(tmp_path: Path) -> Path:
    d = tmp_path / "watched"
    d.mkdir()
    return d


# ---- Path → volume mapping (no hub needed) -------------------------------


def test_volume_for_returns_match(watch_dir: Path) -> None:
    daemon = WatchdogDaemon(_make_host_cfg(watch_dir), hub=_NoopHub())  # type: ignore[arg-type]
    (watch_dir / "sub").mkdir()
    target = watch_dir / "sub" / "file.txt"
    target.write_text("hi")

    match = daemon._volume_for(target)
    assert match is not None
    vol, rel = match
    assert vol.id == "testhost:/work"
    assert rel == "sub/file.txt"


def test_volume_for_returns_none_outside_map(watch_dir: Path) -> None:
    daemon = WatchdogDaemon(_make_host_cfg(watch_dir), hub=_NoopHub())  # type: ignore[arg-type]
    # /etc is reliably outside the test's tmp tree
    assert daemon._volume_for(Path("/etc/hostname")) is None


# ---- End-to-end against a TestClient hub ---------------------------------


def test_process_one_registers_file(watch_dir: Path, real_client: TestClient) -> None:
    daemon = WatchdogDaemon(_make_host_cfg(watch_dir), hub=_TestClientHub(real_client))

    target = watch_dir / "hello.txt"
    target.write_bytes(b"hello world")

    daemon._process_one(str(target))

    listed = real_client.get("/files").json()
    assert len(listed) == 1
    row = listed[0]
    assert row["storage_volume"] == "testhost:/work"
    assert row["relative_path"] == "hello.txt"
    assert row["size"] == len(b"hello world")
    assert row["sha256_status"] == "pending"


def test_process_one_skips_denied(watch_dir: Path, real_client: TestClient) -> None:
    daemon = WatchdogDaemon(_make_host_cfg(watch_dir), hub=_TestClientHub(real_client))

    git_dir = watch_dir / ".git" / "objects"
    git_dir.mkdir(parents=True)
    git_file = git_dir / "abc"
    git_file.write_bytes(b"x")

    daemon._process_one(str(git_file))

    assert real_client.get("/files").json() == []


def test_process_one_outside_volume_map_skipped(watch_dir: Path, tmp_path: Path, real_client: TestClient) -> None:
    other = tmp_path / "other-mount"
    other.mkdir()
    cfg = HostConfig(
        host_id="testhost",
        hub_url="http://test",
        volumes=[VolumeSpec(id="x:/other", mount=str(other), shared=False)],
        watch=WatchSpec(roots=[str(watch_dir)], deny=[]),
    )
    daemon = WatchdogDaemon(cfg, hub=_TestClientHub(real_client))

    stray = watch_dir / "stray.txt"
    stray.write_text("x")

    daemon._process_one(str(stray))
    assert real_client.get("/files").json() == []


def test_dedup_through_real_post(watch_dir: Path, real_client: TestClient) -> None:
    daemon = WatchdogDaemon(_make_host_cfg(watch_dir), hub=_TestClientHub(real_client))

    target = watch_dir / "dup.txt"
    target.write_bytes(b"content")

    daemon._process_one(str(target))
    daemon._process_one(str(target))

    listed = real_client.get("/files").json()
    assert len(listed) == 1


def test_sha256_backfill_one_cycle(watch_dir: Path, real_client: TestClient) -> None:
    daemon = WatchdogDaemon(_make_host_cfg(watch_dir), hub=_TestClientHub(real_client))

    target = watch_dir / "to-hash.txt"
    target.write_bytes(b"hash me fully")
    daemon._process_one(str(target))

    pending = real_client.get("/files", params={"sha256_status": "pending"}).json()
    assert len(pending) == 1

    processed = daemon._sha256_one_cycle({"testhost:/work"})
    assert processed == 1

    computed = real_client.get("/files", params={"sha256_status": "computed"}).json()
    assert len(computed) == 1
    assert computed[0]["sha256"] is not None
    assert len(computed[0]["sha256"]) == 64


def test_sha256_backfill_missing_file_marked_skipped(watch_dir: Path, real_client: TestClient) -> None:
    daemon = WatchdogDaemon(_make_host_cfg(watch_dir), hub=_TestClientHub(real_client))

    target = watch_dir / "vanishing.txt"
    target.write_bytes(b"x")
    daemon._process_one(str(target))
    target.unlink()

    daemon._sha256_one_cycle({"testhost:/work"})

    skipped = real_client.get("/files", params={"sha256_status": "skipped"}).json()
    assert len(skipped) == 1
