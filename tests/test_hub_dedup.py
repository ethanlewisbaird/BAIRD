"""Tests for the hub's file-registration dedup logic."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _file_payload(
    *,
    volume: str = "workstation:/data",
    path: str = "projects/x/out.txt",
    size: int = 100,
    mtime_ns: int = 1_700_000_000_000_000_000,
    head_hash: str = "a" * 64,
    tail_hash: str = "b" * 64,
    sha256: str | None = None,
) -> dict:
    return {
        "storage_volume": volume,
        "relative_path": path,
        "size": size,
        "mtime_ns": mtime_ns,
        "head_hash": head_hash,
        "tail_hash": tail_hash,
        "sha256": sha256,
    }


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_register_new_file(client: TestClient) -> None:
    r = client.post("/files", json=_file_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["id"]
    assert body["sha256_status"] == "pending"


def test_dedup_same_fingerprint_updates_last_seen(client: TestClient) -> None:
    """Posting the same (volume, path, fingerprint) twice returns the same row."""
    payload = _file_payload()
    r1 = client.post("/files", json=payload)
    r2 = client.post("/files", json=payload)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]


def test_modified_file_supersedes_old(client: TestClient) -> None:
    """When the same path is posted with a different fingerprint, a new row is created and the old one soft-deleted."""
    p1 = _file_payload(size=100, head_hash="a" * 64)
    p2 = _file_payload(size=200, head_hash="c" * 64)  # different content

    r1 = client.post("/files", json=p1)
    r2 = client.post("/files", json=p2)

    id1, id2 = r1.json()["id"], r2.json()["id"]
    assert id1 != id2

    # Default list excludes deleted; should see only the new row at this path.
    listed = client.get("/files").json()
    assert [f["id"] for f in listed] == [id2]

    # With include_deleted=True we see both.
    all_listed = client.get("/files", params={"include_deleted": True}).json()
    ids = {f["id"] for f in all_listed}
    assert ids == {id1, id2}


def test_sha256_can_be_filled_via_dedup_post(client: TestClient) -> None:
    """If a later POST carries a sha256 and matches the existing fingerprint, the sha256 fills in."""
    r1 = client.post("/files", json=_file_payload(sha256=None))
    assert r1.json()["sha256_status"] == "pending"

    sha = "9" * 64
    r2 = client.post("/files", json=_file_payload(sha256=sha))
    assert r2.json()["id"] == r1.json()["id"]
    assert r2.json()["sha256"] == sha
    assert r2.json()["sha256_status"] == "computed"


def test_patch_sha256(client: TestClient) -> None:
    """The backfill worker uses PATCH /files/{id} to fill in the full hash."""
    r1 = client.post("/files", json=_file_payload())
    file_id = r1.json()["id"]

    sha = "f" * 64
    r2 = client.patch(f"/files/{file_id}", json={"sha256": sha, "sha256_status": "computed"})
    assert r2.status_code == 200
    assert r2.json()["sha256"] == sha
    assert r2.json()["sha256_status"] == "computed"


def test_patch_skipped(client: TestClient) -> None:
    r1 = client.post("/files", json=_file_payload())
    file_id = r1.json()["id"]
    r2 = client.patch(f"/files/{file_id}", json={"sha256_status": "skipped"})
    assert r2.status_code == 200
    assert r2.json()["sha256_status"] == "skipped"


def test_list_filters(client: TestClient) -> None:
    client.post("/files", json=_file_payload(volume="vol-a", path="a.txt"))
    client.post("/files", json=_file_payload(volume="vol-a", path="b.txt", head_hash="b" * 64))
    client.post("/files", json=_file_payload(volume="vol-b", path="c.txt", head_hash="c" * 64))

    only_a = client.get("/files", params={"storage_volume": "vol-a"}).json()
    assert {f["relative_path"] for f in only_a} == {"a.txt", "b.txt"}

    pending = client.get("/files", params={"sha256_status": "pending"}).json()
    assert len(pending) == 3


def test_get_404(client: TestClient) -> None:
    r = client.get("/files/does-not-exist")
    assert r.status_code == 404


def test_patch_404(client: TestClient) -> None:
    r = client.patch("/files/does-not-exist", json={"sha256_status": "computed"})
    assert r.status_code == 404
