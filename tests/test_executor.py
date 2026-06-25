"""Tests for the satellite executor service."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baird.config import HostConfig, VolumeSpec, WatchSpec
from baird.executor import create_executor_app


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=root, check=True)
    return root


@pytest.fixture
def host_cfg(tmp_path: Path) -> HostConfig:
    return HostConfig(
        host_id="testhost",
        hub_url="http://test",
        auth_token="secret-token",
        volumes=[VolumeSpec(id="test:/", mount=str(tmp_path), shared=False)],
        watch=WatchSpec(roots=[str(tmp_path)], deny=[]),
    )


@pytest.fixture
def client(host_cfg: HostConfig) -> TestClient:
    return TestClient(create_executor_app(host_cfg))


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret-token"}


# ---- Auth --------------------------------------------------------------


def test_health_requires_token(client: TestClient) -> None:
    assert client.get("/exec/health").status_code == 401
    assert client.get("/exec/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.get("/exec/health", headers=_auth())
    assert r.status_code == 200
    assert r.json()["host_id"] == "testhost"


def test_no_token_configured_blocks_everything(tmp_path: Path) -> None:
    cfg = HostConfig(
        host_id="testhost",
        hub_url="http://test",
        auth_token=None,
        volumes=[VolumeSpec(id="test:/", mount=str(tmp_path), shared=False)],
        watch=WatchSpec(roots=[str(tmp_path)], deny=[]),
    )
    c = TestClient(create_executor_app(cfg))
    r = c.get("/exec/health", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 403


# ---- read_file ---------------------------------------------------------


def test_read_file(client: TestClient, tmp_path: Path) -> None:
    f = tmp_path / "hello.txt"
    f.write_text("hi")
    r = client.post("/exec/read_file", json={"path": str(f)}, headers=_auth())
    assert r.status_code == 200
    assert r.json()["content"] == "hi"


def test_read_file_outside_mount_rejected(client: TestClient) -> None:
    r = client.post("/exec/read_file", json={"path": "/etc/hostname"}, headers=_auth())
    assert r.status_code == 403


def test_read_missing_file_404(client: TestClient, tmp_path: Path) -> None:
    r = client.post("/exec/read_file", json={"path": str(tmp_path / "ghost.txt")}, headers=_auth())
    assert r.status_code == 404


# ---- write_file --------------------------------------------------------


def test_write_file_inside_project(client: TestClient, project_root: Path) -> None:
    target = project_root / "src" / "x.py"
    r = client.post(
        "/exec/write_file",
        json={
            "path": str(target),
            "content": "print('hi')\n",
            "project_root": str(project_root),
        },
        headers=_auth(),
    )
    assert r.status_code == 200
    assert target.read_text() == "print('hi')\n"


def test_write_file_outside_project_rejected(
    client: TestClient, project_root: Path, tmp_path: Path
) -> None:
    # tmp_path is the mount; project_root is a subdir.
    other = tmp_path / "outside.txt"
    r = client.post(
        "/exec/write_file",
        json={
            "path": str(other),
            "content": "x",
            "project_root": str(project_root),
        },
        headers=_auth(),
    )
    assert r.status_code == 403


# ---- run_command -------------------------------------------------------


def test_run_command_safe(client: TestClient, tmp_path: Path) -> None:
    r = client.post(
        "/exec/run_command",
        json={"command": "echo hello", "cwd": str(tmp_path)},
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["exit_code"] == 0
    assert "hello" in body["stdout"]
    assert body["tier"] == "safe"


def test_run_command_destructive_rejected(client: TestClient, tmp_path: Path) -> None:
    r = client.post(
        "/exec/run_command",
        json={"command": "rm file.txt", "cwd": str(tmp_path)},
        headers=_auth(),
    )
    assert r.status_code == 403


def test_run_command_project_override_accepted(client: TestClient, tmp_path: Path) -> None:
    r = client.post(
        "/exec/run_command",
        json={
            "command": "./my_runner.sh",
            "cwd": str(tmp_path),
            "project_overrides": [
                {"command_regex": "^./my_runner\\.sh", "tier": "project"}
            ],
        },
        headers=_auth(),
    )
    # The command will probably fail (file doesn't exist), but it should be
    # classified-and-attempted rather than rejected.
    assert r.status_code in {200, 504}


def test_run_command_unknown_cwd_rejected(client: TestClient) -> None:
    r = client.post(
        "/exec/run_command",
        json={"command": "ls", "cwd": "/etc"},
        headers=_auth(),
    )
    assert r.status_code == 403


# ---- apply_diff --------------------------------------------------------


def test_apply_diff_creates_commit(client: TestClient, project_root: Path) -> None:
    # Seed an initial commit.
    (project_root / "README.md").write_text("v1\n")
    subprocess.run(["git", "add", "."], cwd=project_root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=project_root, check=True)

    diff = (
        "diff --git a/README.md b/README.md\n"
        "index e69de29..d00491f 100644\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1 @@\n"
        "-v1\n"
        "+v2\n"
    )
    r = client.post(
        "/exec/apply_diff",
        json={
            "project_root": str(project_root),
            "diff": diff,
            "commit_message": "bump version",
        },
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "README.md" in body["files_changed"]
    assert (project_root / "README.md").read_text() == "v2\n"


def test_apply_diff_rejects_non_git_root(client: TestClient, tmp_path: Path) -> None:
    not_a_repo = tmp_path / "notrepo"
    not_a_repo.mkdir()
    r = client.post(
        "/exec/apply_diff",
        json={"project_root": str(not_a_repo), "diff": "", "commit_message": "x"},
        headers=_auth(),
    )
    assert r.status_code == 400
