"""HTTP client for the satellite executor (Phase 3).

Wraps the four executor routes (`read_file`, `write_file`, `run_command`,
`apply_diff`) and bakes in the `project.yaml` `permissions:` override loading
so callers don't have to repeat it.

Used by the orchestrator-side dispatcher when a task's `runnable.host_id` ≠ hub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .permissions import overrides_from_project_yaml
from .project_yaml import load_project_yaml


def _load_overrides(project_root: Path | str | None) -> list[dict]:
    """Read `<project_root>/.baird/project.yaml` and pull out the
    `permissions:` overrides, ready to drop into the executor request body."""
    if project_root is None:
        return []
    root = Path(project_root)
    pj = root / ".baird" / "project.yaml"
    if not pj.exists():
        return []
    try:
        py = load_project_yaml(pj)
    except Exception:
        return []
    py_dict = py.model_dump()
    return [
        {"command_regex": o.command_regex, "tier": o.tier.value, "reason": o.reason}
        for o in overrides_from_project_yaml(py_dict)
    ]


class ExecutorClient:
    """Talks to one satellite's executor.

    Pass the executor URL (e.g. `http://127.0.0.1:8766` when the hub reaches
    the satellite via SSH forward tunnel) and the bearer token the satellite's
    executor expects on its inbound calls.
    """

    def __init__(self, base_url: str, auth_token: str, *, timeout: float = 60.0):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ExecutorClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def health(self) -> dict:
        r = self._client.get("/exec/health")
        r.raise_for_status()
        return r.json()

    def read_file(self, path: str) -> dict:
        r = self._client.post("/exec/read_file", json={"path": path})
        r.raise_for_status()
        return r.json()

    def write_file(
        self,
        path: str,
        content: str,
        *,
        project_root: str | None = None,
        create_parents: bool = True,
    ) -> dict:
        r = self._client.post(
            "/exec/write_file",
            json={
                "path": path,
                "content": content,
                "project_root": project_root,
                "create_parents": create_parents,
            },
        )
        r.raise_for_status()
        return r.json()

    def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        project_root: str | Path | None = None,
        timeout_s: float = 30.0,
    ) -> dict:
        """Run a shell command on the satellite. Loads `project.yaml`
        permissions: overrides automatically — the satellite enforces them."""
        body: dict[str, Any] = {
            "command": command,
            "cwd": cwd,
            "timeout_s": timeout_s,
            "project_root": str(project_root) if project_root else None,
            "project_overrides": _load_overrides(project_root),
        }
        r = self._client.post("/exec/run_command", json=body)
        r.raise_for_status()
        return r.json()

    def apply_diff(
        self,
        diff: str,
        *,
        project_root: str,
        commit_message: str,
        allow_dirty_outside_targets: bool = True,
    ) -> dict:
        r = self._client.post(
            "/exec/apply_diff",
            json={
                "diff": diff,
                "project_root": project_root,
                "commit_message": commit_message,
                "allow_dirty_outside_targets": allow_dirty_outside_targets,
            },
        )
        r.raise_for_status()
        return r.json()
