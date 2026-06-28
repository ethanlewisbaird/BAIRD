"""Client library for the hub's REST API.

The narrow public surface designed in Phase 2 — projects, decisions, actions
(with a `start_action` context manager), file lineage, sessions/messages,
notifications, and a unified `recall` — implemented over httpx.

Callers (the watchdog daemon, the CLI, the orchestrator) should use this
client rather than hitting routes directly so the API surface is the only
contract that needs to stay stable.
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from typing import Any, Iterator

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

    # ---- Health ----

    def health(self) -> dict:
        r = self._client.get("/health")
        r.raise_for_status()
        return r.json()

    # ---- Registry: files ----

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

    def list_files(
        self,
        *,
        sha256_status: str | None = None,
        storage_volume: str | None = None,
        include_deleted: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        params: dict[str, object] = {
            "include_deleted": include_deleted,
            "limit": limit,
        }
        if sha256_status:
            params["sha256_status"] = sha256_status
        if storage_volume:
            params["storage_volume"] = storage_volume
        r = self._client.get("/files", params=params)
        r.raise_for_status()
        return r.json()

    def patch_file(
        self,
        file_id: str,
        *,
        sha256: str | None = None,
        sha256_status: str | None = None,
    ) -> dict:
        body: dict[str, object] = {}
        if sha256 is not None:
            body["sha256"] = sha256
        if sha256_status is not None:
            body["sha256_status"] = sha256_status
        r = self._client.patch(f"/files/{file_id}", json=body)
        r.raise_for_status()
        return r.json()

    # ---- Projects ----

    def upsert_project(
        self,
        *,
        id: str,
        name: str,
        github: str | None = None,
        context: str | None = None,
        parent_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict:
        r = self._client.post(
            "/projects",
            json={
                "id": id,
                "name": name,
                "github": github,
                "context": context,
                "parent_id": parent_id,
                "config": config or {},
            },
        )
        r.raise_for_status()
        return r.json()

    def get_project(self, project_id: str) -> dict:
        r = self._client.get(f"/projects/{project_id}")
        r.raise_for_status()
        return r.json()

    def list_projects(self) -> list[dict]:
        r = self._client.get("/projects")
        r.raise_for_status()
        return r.json()

    def list_children(self, project_id: str) -> list[dict]:
        """List immediate children of a project (one-level hierarchy)."""
        r = self._client.get(f"/projects/{project_id}/children")
        r.raise_for_status()
        return r.json()

    def patch_project(self, project_id: str, **fields: Any) -> dict:
        """Partial update of a project. Server accepts `name`, `github`,
        `context`, `parent_id`, and `config` — only fields supplied here
        are touched on the row."""
        r = self._client.patch(f"/projects/{project_id}", json=fields)
        r.raise_for_status()
        return r.json()

    def rename_project(self, project_id: str, new_name: str) -> dict:
        """Convenience wrapper around `patch_project` for the common
        rename case (issue #3)."""
        return self.patch_project(project_id, name=new_name)

    def delete_project(self, project_id: str) -> dict:
        """Hard delete. Server rejects with 400 if the project has
        children — caller must reparent or delete them first. Decisions,
        sessions, actions referencing the project are left in place
        (historical record; the FK is plain string, no DB cascade)."""
        r = self._client.delete(f"/projects/{project_id}")
        r.raise_for_status()
        return r.json()

    # ---- Project locations ----

    def list_project_locations(self, project_id: str) -> list[dict]:
        r = self._client.get(f"/projects/{project_id}/locations")
        r.raise_for_status()
        return r.json()

    def add_project_location(
        self, project_id: str, *, host: str, path: str, role: str | None = None
    ) -> list[dict]:
        r = self._client.post(
            f"/projects/{project_id}/locations",
            json={"host": host, "path": path, "role": role},
        )
        r.raise_for_status()
        return r.json()

    def remove_project_location(
        self, project_id: str, *, host: str, path: str
    ) -> list[dict]:
        r = self._client.request(
            "DELETE",
            f"/projects/{project_id}/locations",
            params={"host": host, "path": path},
        )
        r.raise_for_status()
        return r.json()

    # ---- Decisions ----

    def record_decision(self, project_id: str, text: str, *, author: str = "user") -> dict:
        r = self._client.post(
            f"/projects/{project_id}/decisions",
            json={"project_id": project_id, "text": text, "author": author},
        )
        r.raise_for_status()
        return r.json()

    def list_decisions(self, project_id: str, *, limit: int = 50) -> list[dict]:
        r = self._client.get(
            f"/projects/{project_id}/decisions", params={"limit": limit}
        )
        r.raise_for_status()
        return r.json()

    # ---- Actions ----

    def create_action(self, **fields: Any) -> dict:
        r = self._client.post("/actions", json=fields)
        r.raise_for_status()
        return r.json()

    def finish_action(
        self,
        action_id: str,
        *,
        exit_code: int | None = None,
        summary: str | None = None,
        finished_at: dt.datetime | None = None,
        cost_usd: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if exit_code is not None:
            body["exit_code"] = exit_code
        if summary is not None:
            body["summary"] = summary
        if cost_usd is not None:
            body["cost_usd"] = cost_usd
        if input_tokens is not None:
            body["input_tokens"] = input_tokens
        if output_tokens is not None:
            body["output_tokens"] = output_tokens
        body["finished_at"] = (finished_at or dt.datetime.now(dt.timezone.utc)).isoformat()
        r = self._client.patch(f"/actions/{action_id}", json=body)
        r.raise_for_status()
        return r.json()

    def list_actions(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        started_after: dt.datetime | None = None,
        unfinished_only: bool = False,
        limit: int = 50,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "unfinished_only": unfinished_only}
        if project_id:
            params["project_id"] = project_id
        if task_id:
            params["task_id"] = task_id
        if started_after is not None:
            params["started_after"] = started_after.isoformat()
        r = self._client.get("/actions", params=params)
        r.raise_for_status()
        return r.json()

    def stats(self) -> dict:
        r = self._client.get("/stats")
        r.raise_for_status()
        return r.json()

    def budgets_usage(self, *, since_hours: int = 24, task_id: str | None = None) -> dict:
        params: dict[str, Any] = {"since_hours": since_hours}
        if task_id:
            params["task_id"] = task_id
        r = self._client.get("/budgets/usage", params=params)
        r.raise_for_status()
        return r.json()

    def get_action(self, action_id: str) -> dict:
        r = self._client.get(f"/actions/{action_id}")
        r.raise_for_status()
        return r.json()

    def attach_file(self, action_id: str, file_id: str, role: str) -> dict:
        r = self._client.post(
            f"/actions/{action_id}/files",
            json={"file_id": file_id, "role": role},
        )
        r.raise_for_status()
        return r.json()

    def file_lineage(self, file_id: str) -> dict:
        r = self._client.get(f"/files/{file_id}/lineage")
        r.raise_for_status()
        return r.json()

    # ---- Events ----

    def emit_event(self, name: str, payload: dict[str, Any] | None = None) -> dict:
        r = self._client.post(f"/events/{name}", json=payload or {})
        r.raise_for_status()
        return r.json()

    def list_events(self, *, unconsumed_only: bool = False, limit: int = 50) -> list[dict]:
        r = self._client.get(
            "/events", params={"unconsumed_only": unconsumed_only, "limit": limit}
        )
        r.raise_for_status()
        return r.json()

    def consume_event(self, event_id: str) -> dict:
        r = self._client.post(f"/events/{event_id}/consume")
        r.raise_for_status()
        return r.json()

    # ---- Recall promotion ----

    def flag_action(
        self, *, action_id: str, text: str, project_id: str | None = None
    ) -> dict | None:
        """Promote a tier-3 fragment for this action. Returns None when the
        hub's recall index isn't configured."""
        r = self._client.post(
            "/recall/flag",
            json={"action_id": action_id, "text": text, "project_id": project_id},
        )
        r.raise_for_status()
        body = r.json()
        return body if body.get("id") else None

    def resolve_pair(
        self,
        *,
        error_action_id: str,
        fix_action_id: str,
        text: str,
        project_id: str | None = None,
    ) -> dict | None:
        r = self._client.post(
            "/recall/resolve",
            json={
                "error_action_id": error_action_id,
                "fix_action_id": fix_action_id,
                "text": text,
                "project_id": project_id,
            },
        )
        r.raise_for_status()
        body = r.json()
        return body if body.get("id") else None

    @contextmanager
    def start_action(self, **fields: Any) -> Iterator["ActionHandle"]:
        """Context manager: opens an action row, lets the caller record I/O,
        a summary, and usage/cost, and patches the finish state on exit. Exit
        code defaults to 0 on clean exit, 1 if the block raised."""
        action = self.create_action(**fields)
        handle = ActionHandle(self, action)
        try:
            yield handle
        except Exception:
            if handle._exit_code is None:
                handle._exit_code = 1
            self._finish_from_handle(handle)
            raise
        else:
            if handle._exit_code is None:
                handle._exit_code = 0
            self._finish_from_handle(handle)

    def _finish_from_handle(self, handle: "ActionHandle") -> None:
        self.finish_action(
            handle.id,
            exit_code=handle._exit_code,
            summary=handle._summary,
            cost_usd=handle._cost_usd,
            input_tokens=handle._input_tokens,
            output_tokens=handle._output_tokens,
        )

    # ---- Sessions + messages ----

    def new_session(
        self, *, mode: str = "code", project_id: str | None = None, task_id: str | None = None
    ) -> dict:
        r = self._client.post(
            "/sessions",
            json={"mode": mode, "project_id": project_id, "task_id": task_id},
        )
        r.raise_for_status()
        return r.json()

    def list_sessions(
        self,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        mode: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if task_id:
            params["task_id"] = task_id
        if project_id:
            params["project_id"] = project_id
        if mode:
            params["mode"] = mode
        r = self._client.get("/sessions", params=params)
        r.raise_for_status()
        return r.json()

    def find_or_create_session_for_task(
        self, *, task_id: str, project_id: str | None = None, mode: str = "agent"
    ) -> dict:
        existing = self.list_sessions(task_id=task_id, mode=mode, limit=1)
        if existing:
            return existing[0]
        return self.new_session(mode=mode, task_id=task_id, project_id=project_id)

    def append_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        tool_calls: dict | None = None,
    ) -> dict:
        r = self._client.post(
            f"/sessions/{session_id}/messages",
            json={"role": role, "content": content, "tool_calls": tool_calls},
        )
        r.raise_for_status()
        return r.json()

    def get_messages(
        self, session_id: str, *, limit: int = 200, offset: int = 0
    ) -> list[dict]:
        r = self._client.get(
            f"/sessions/{session_id}/messages",
            params={"limit": limit, "offset": offset},
        )
        r.raise_for_status()
        return r.json()

    # ---- Notifications ----

    def create_notification(
        self,
        *,
        kind: str,
        title: str,
        body: str | None = None,
        project_id: str | None = None,
        action_id: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        r = self._client.post(
            "/notifications",
            json={
                "kind": kind,
                "title": title,
                "body": body,
                "project_id": project_id,
                "action_id": action_id,
                "task_id": task_id,
            },
        )
        r.raise_for_status()
        return r.json()

    def list_notifications(
        self, *, unresolved_only: bool = False, limit: int = 100
    ) -> list[dict]:
        r = self._client.get(
            "/notifications",
            params={"unresolved_only": unresolved_only, "limit": limit},
        )
        r.raise_for_status()
        return r.json()

    def resolve_notification(
        self, notif_id: str, *, resolution: str, mark_read: bool = True
    ) -> dict:
        r = self._client.patch(
            f"/notifications/{notif_id}",
            json={"resolution": resolution, "read": mark_read},
        )
        r.raise_for_status()
        return r.json()

    # ---- Recall ----

    def recall(
        self,
        query: str,
        *,
        project_id: str | None = None,
        k: int = 10,
        sources: list[str] | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"query": query, "k": k}
        if project_id:
            params["project_id"] = project_id
        if sources:
            params["sources"] = ",".join(sources)
        r = self._client.get("/recall", params=params)
        r.raise_for_status()
        return r.json()["hits"]


class ActionHandle:
    """Companion object yielded by `HubClient.start_action`."""

    def __init__(self, client: HubClient, action: dict):
        self.client = client
        self.action = action
        self._exit_code: int | None = None
        self._summary: str | None = None
        self._cost_usd: float | None = None
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None

    @property
    def id(self) -> str:
        return self.action["id"]

    def attach(self, file_id: str, role: str) -> dict:
        return self.client.attach_file(self.id, file_id, role)

    def set_summary(self, text: str) -> None:
        self._summary = text

    def set_exit_code(self, code: int) -> None:
        self._exit_code = code

    def record_usage(
        self,
        *,
        cost_usd: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        if cost_usd is not None:
            self._cost_usd = (self._cost_usd or 0.0) + cost_usd
        if input_tokens is not None:
            self._input_tokens = (self._input_tokens or 0) + input_tokens
        if output_tokens is not None:
            self._output_tokens = (self._output_tokens or 0) + output_tokens
