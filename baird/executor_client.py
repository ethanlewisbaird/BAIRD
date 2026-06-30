"""HTTP client for the satellite executor (Phase 3).

Wraps the four executor routes (`read_file`, `write_file`, `run_command`,
`apply_diff`) and bakes in the `project.yaml` `permissions:` override loading
so callers don't have to repeat it.

Used by the orchestrator-side dispatcher when a task's `runnable.host_id` ≠ hub.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx

from .permissions import overrides_from_project_yaml
from .project_yaml import load_project_yaml


log = logging.getLogger(__name__)

# Transport errors worth retrying — SSH tunnels can briefly drop during reconnect.
_RETRYABLE = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.TransportError,
)


def _executor_retry(op, *, attempts=3, backoff_s=(1.0, 3.0), label=""):
    """Call `op`; on retryable network errors sleep + retry up to `attempts`."""
    for i in range(attempts):
        try:
            return op()
        except _RETRYABLE as e:
            remaining = attempts - i - 1
            if remaining == 0:
                raise
            wait = backoff_s[min(i, len(backoff_s) - 1)]
            log.warning(
                "executor_client: %s failed (%s); retrying in %.1fs (%d left)",
                label or "call", e.__class__.__name__, wait, remaining,
            )
            time.sleep(wait)


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

    def _call(self, method: str, path: str, json: dict | None = None) -> Any:
        """POST with retry on transport errors."""
        def _do():
            r = self._client.request(method, path, json=json)
            r.raise_for_status()
            return r.json()
        return _executor_retry(_do, label=f"{method} {path}")

    def health(self) -> dict:
        return self._call("GET", "/exec/health")

    def read_file(self, path: str) -> dict:
        return self._call("POST", "/exec/read_file", json={"path": path})

    def write_file(
        self,
        path: str,
        content: str,
        *,
        project_root: str | None = None,
        create_parents: bool = True,
    ) -> dict:
        return self._call("POST", "/exec/write_file", json={
            "path": path,
            "content": content,
            "project_root": project_root,
            "create_parents": create_parents,
        })

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
        return self._call("POST", "/exec/run_command", json=body)

    def apply_diff(
        self,
        diff: str,
        *,
        project_root: str,
        commit_message: str,
        allow_dirty_outside_targets: bool = True,
    ) -> dict:
        return self._call("POST", "/exec/apply_diff", json={
            "diff": diff,
            "project_root": project_root,
            "commit_message": commit_message,
            "allow_dirty_outside_targets": allow_dirty_outside_targets,
        })
