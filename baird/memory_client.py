"""Client library for the hub's REST API.

The narrow public surface designed in Phase 2 — `record_decision`, `start_action`,
`recall`, `register_file`, etc. — gets implemented here on top of httpx.

This is a skeleton; full surface filled in during Phase 2 implementation.
"""

from __future__ import annotations

import httpx


class HubClient:
    def __init__(self, base_url: str, auth_token: str | None = None, timeout: float = 10.0):
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
        self._client = httpx.Client(base_url=base_url, headers=headers, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HubClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- Registry ----

    def health(self) -> dict:
        r = self._client.get("/health")
        r.raise_for_status()
        return r.json()

    def register_file(
        self,
        *,
        storage_volume: str,
        relative_path: str,
        size: int,
        mtime_ns: int,
        head_hash: str,
        tail_hash: str,
        sha256: str | None = None,
    ) -> dict:
        r = self._client.post(
            "/files",
            json={
                "storage_volume": storage_volume,
                "relative_path": relative_path,
                "size": size,
                "mtime_ns": mtime_ns,
                "head_hash": head_hash,
                "tail_hash": tail_hash,
                "sha256": sha256,
            },
        )
        r.raise_for_status()
        return r.json()

    def get_file(self, file_id: str) -> dict:
        r = self._client.get(f"/files/{file_id}")
        r.raise_for_status()
        return r.json()

    # TODO Phase 2: record_decision, start_action, recall, etc.
