"""Phase 2 hub routes — projects, decisions, actions, file lineage, sessions,
messages, notifications, and a lightweight `recall` endpoint.

Wired into the app by `hub.create_app()` so the routes share the same
`get_registry` / `get_memory` dependencies.

Recall is SQL-backed for now (LIKE over action summaries + decision text +
notification bodies). LanceDB replaces the search backend in a later slice —
the response shape stays the same.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from .db import (
    Action,
    Decision,
    File,
    FileAction,
    Message,
    Notification,
    Project,
)
from .db import Session as SessionRow
from .hub import get_memory, get_registry


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ----- Schemas -----------------------------------------------------------


class ProjectIn(BaseModel):
    id: str
    name: str
    github: Optional[str] = None
    context: Optional[str] = None
    config: dict[str, Any] = Field(default_factory=dict)


class ProjectOut(ProjectIn):
    created_at: dt.datetime


class DecisionIn(BaseModel):
    project_id: str
    text: str
    author: str = "user"  # user | ai


class DecisionOut(DecisionIn):
    id: str
    created_at: dt.datetime


class ActionStart(BaseModel):
    project_id: Optional[str] = None
    parent_action_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_version: Optional[str] = None
    command: Optional[str] = None
    host: Optional[str] = None
    conda_env: Optional[str] = None
    env_hash: Optional[str] = None
    slurm_job_id: Optional[str] = None


class ActionPatch(BaseModel):
    finished_at: Optional[dt.datetime] = None
    exit_code: Optional[int] = None
    summary: Optional[str] = None


class ActionOut(BaseModel):
    id: str
    project_id: Optional[str]
    parent_action_id: Optional[str]
    tool_name: Optional[str]
    tool_version: Optional[str]
    command: Optional[str]
    host: Optional[str]
    conda_env: Optional[str]
    env_hash: Optional[str]
    started_at: dt.datetime
    finished_at: Optional[dt.datetime]
    exit_code: Optional[int]
    slurm_job_id: Optional[str]
    summary: Optional[str]


class FileActionIn(BaseModel):
    file_id: str
    role: str  # input | output | log


class FileActionOut(FileActionIn):
    id: str
    action_id: str


class SessionIn(BaseModel):
    project_id: Optional[str] = None
    mode: str = "code"  # code | chat | agent
    task_id: Optional[str] = None


class SessionOut(SessionIn):
    id: str
    started_at: dt.datetime
    last_active_at: dt.datetime


class MessageIn(BaseModel):
    role: str
    content: str
    tool_calls: Optional[dict[str, Any]] = None


class MessageOut(MessageIn):
    id: str
    session_id: str
    created_at: dt.datetime


class NotificationIn(BaseModel):
    kind: str
    title: str
    body: Optional[str] = None
    project_id: Optional[str] = None
    action_id: Optional[str] = None
    task_id: Optional[str] = None


class NotificationOut(NotificationIn):
    id: str
    created_at: dt.datetime
    read_at: Optional[dt.datetime]
    resolved_at: Optional[dt.datetime]
    resolution: Optional[str]


class NotificationPatch(BaseModel):
    read: Optional[bool] = None
    resolution: Optional[str] = None


class RecallHit(BaseModel):
    source: str  # action_summary | decision | notification
    id: str
    project_id: Optional[str]
    text: str
    created_at: dt.datetime


class RecallResponse(BaseModel):
    hits: list[RecallHit]


# ----- Conversion helpers ------------------------------------------------


def _action_out(row: Action) -> ActionOut:
    return ActionOut(
        id=row.id,
        project_id=row.project_id,
        parent_action_id=row.parent_action_id,
        tool_name=row.tool_name,
        tool_version=row.tool_version,
        command=row.command,
        host=row.host,
        conda_env=row.conda_env,
        env_hash=row.env_hash,
        started_at=row.started_at,
        finished_at=row.finished_at,
        exit_code=row.exit_code,
        slurm_job_id=row.slurm_job_id,
        summary=row.summary,
    )


def _project_out(row: Project) -> ProjectOut:
    return ProjectOut(
        id=row.id,
        name=row.name,
        github=row.github,
        context=row.context,
        config=row.config or {},
        created_at=row.created_at,
    )


# ----- Route registration ------------------------------------------------


def register_routes(app: FastAPI) -> None:

    # ---- Projects ----

    @app.post("/projects", response_model=ProjectOut)
    def upsert_project(payload: ProjectIn, s: Session = Depends(get_memory)) -> ProjectOut:
        row = s.get(Project, payload.id)
        if row is None:
            row = Project(
                id=payload.id,
                name=payload.name,
                github=payload.github,
                context=payload.context,
                config=payload.config,
            )
            s.add(row)
        else:
            row.name = payload.name
            row.github = payload.github
            row.context = payload.context
            row.config = payload.config
        s.commit()
        s.refresh(row)
        return _project_out(row)

    @app.get("/projects", response_model=list[ProjectOut])
    def list_projects(s: Session = Depends(get_memory)) -> list:
        rows = s.scalars(select(Project).order_by(Project.created_at)).all()
        return [_project_out(r) for r in rows]

    @app.get("/projects/{project_id}", response_model=ProjectOut)
    def get_project(project_id: str, s: Session = Depends(get_memory)) -> ProjectOut:
        row = s.get(Project, project_id)
        if row is None:
            raise HTTPException(404, "project not found")
        return _project_out(row)

    # ---- Decisions ----

    @app.post("/projects/{project_id}/decisions", response_model=DecisionOut)
    def record_decision(
        project_id: str, payload: DecisionIn, s: Session = Depends(get_memory)
    ) -> DecisionOut:
        if payload.project_id != project_id:
            raise HTTPException(400, "project_id mismatch")
        if payload.author not in {"user", "ai"}:
            raise HTTPException(400, "author must be 'user' or 'ai'")
        if s.get(Project, project_id) is None:
            raise HTTPException(404, "project not found")
        row = Decision(project_id=project_id, text=payload.text, author=payload.author)
        s.add(row)
        s.commit()
        s.refresh(row)
        return DecisionOut(
            id=row.id,
            project_id=row.project_id,
            text=row.text,
            author=row.author,
            created_at=row.created_at,
        )

    @app.get("/projects/{project_id}/decisions", response_model=list[DecisionOut])
    def list_decisions(
        project_id: str,
        limit: int = Query(50, le=500),
        s: Session = Depends(get_memory),
    ) -> list:
        rows = s.scalars(
            select(Decision)
            .where(Decision.project_id == project_id)
            .order_by(desc(Decision.created_at))
            .limit(limit)
        ).all()
        return [
            DecisionOut(
                id=r.id,
                project_id=r.project_id,
                text=r.text,
                author=r.author,
                created_at=r.created_at,
            )
            for r in rows
        ]

    # ---- Actions (live in the registry DB) ----

    @app.post("/actions", response_model=ActionOut)
    def start_action(payload: ActionStart, s: Session = Depends(get_registry)) -> ActionOut:
        row = Action(
            project_id=payload.project_id,
            parent_action_id=payload.parent_action_id,
            tool_name=payload.tool_name,
            tool_version=payload.tool_version,
            command=payload.command,
            host=payload.host,
            conda_env=payload.conda_env,
            env_hash=payload.env_hash,
            slurm_job_id=payload.slurm_job_id,
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return _action_out(row)

    @app.patch("/actions/{action_id}", response_model=ActionOut)
    def finish_action(
        action_id: str, payload: ActionPatch, s: Session = Depends(get_registry)
    ) -> ActionOut:
        row = s.get(Action, action_id)
        if row is None:
            raise HTTPException(404, "action not found")
        if payload.finished_at is not None:
            row.finished_at = payload.finished_at
        if payload.exit_code is not None:
            row.exit_code = payload.exit_code
        if payload.summary is not None:
            row.summary = payload.summary
        s.commit()
        s.refresh(row)
        return _action_out(row)

    @app.get("/actions", response_model=list[ActionOut])
    def list_actions(
        project_id: Optional[str] = Query(None),
        limit: int = Query(50, le=500),
        s: Session = Depends(get_registry),
    ) -> list:
        q = select(Action)
        if project_id is not None:
            q = q.where(Action.project_id == project_id)
        q = q.order_by(desc(Action.started_at)).limit(limit)
        return [_action_out(r) for r in s.scalars(q).all()]

    @app.get("/actions/{action_id}", response_model=ActionOut)
    def get_action(action_id: str, s: Session = Depends(get_registry)) -> ActionOut:
        row = s.get(Action, action_id)
        if row is None:
            raise HTTPException(404, "action not found")
        return _action_out(row)

    @app.post("/actions/{action_id}/files", response_model=FileActionOut)
    def attach_file(
        action_id: str, payload: FileActionIn, s: Session = Depends(get_registry)
    ) -> FileActionOut:
        if payload.role not in {"input", "output", "log"}:
            raise HTTPException(400, "role must be input | output | log")
        if s.get(Action, action_id) is None:
            raise HTTPException(404, "action not found")
        if s.get(File, payload.file_id) is None:
            raise HTTPException(404, "file not found")
        row = FileAction(action_id=action_id, file_id=payload.file_id, role=payload.role)
        s.add(row)
        # Mirror created_by on the file when it's a fresh output.
        if payload.role == "output":
            f = s.get(File, payload.file_id)
            if f is not None and f.created_by_action_id is None:
                f.created_by_action_id = action_id
        s.commit()
        s.refresh(row)
        return FileActionOut(id=row.id, action_id=row.action_id, file_id=row.file_id, role=row.role)

    # ---- File lineage ----

    @app.get("/files/{file_id}/lineage")
    def file_lineage(file_id: str, s: Session = Depends(get_registry)) -> dict:
        f = s.get(File, file_id)
        if f is None:
            raise HTTPException(404, "file not found")
        edges = s.scalars(select(FileAction).where(FileAction.file_id == file_id)).all()
        out: dict[str, Any] = {"file_id": file_id, "actions": []}
        for e in edges:
            a = s.get(Action, e.action_id)
            if a is None:
                continue
            out["actions"].append({
                "action_id": a.id,
                "role": e.role,
                "tool_name": a.tool_name,
                "command": a.command,
                "started_at": a.started_at.isoformat(),
                "summary": a.summary,
            })
        return out

    # ---- Sessions + messages ----

    @app.post("/sessions", response_model=SessionOut)
    def new_session(payload: SessionIn, s: Session = Depends(get_memory)) -> SessionOut:
        if payload.mode not in {"code", "chat", "agent"}:
            raise HTTPException(400, "mode must be code | chat | agent")
        row = SessionRow(
            project_id=payload.project_id, mode=payload.mode, task_id=payload.task_id
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return SessionOut(
            id=row.id,
            project_id=row.project_id,
            mode=row.mode,
            task_id=row.task_id,
            started_at=row.started_at,
            last_active_at=row.last_active_at,
        )

    @app.post("/sessions/{session_id}/messages", response_model=MessageOut)
    def append_message(
        session_id: str, payload: MessageIn, s: Session = Depends(get_memory)
    ) -> MessageOut:
        sess = s.get(SessionRow, session_id)
        if sess is None:
            raise HTTPException(404, "session not found")
        row = Message(
            session_id=session_id,
            role=payload.role,
            content=payload.content,
            tool_calls=payload.tool_calls,
        )
        sess.last_active_at = _utcnow()
        s.add(row)
        s.commit()
        s.refresh(row)
        return MessageOut(
            id=row.id,
            session_id=row.session_id,
            role=row.role,
            content=row.content,
            tool_calls=row.tool_calls,
            created_at=row.created_at,
        )

    @app.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
    def get_messages(
        session_id: str,
        limit: int = Query(200, le=1000),
        s: Session = Depends(get_memory),
    ) -> list:
        if s.get(SessionRow, session_id) is None:
            raise HTTPException(404, "session not found")
        rows = s.scalars(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at)
            .limit(limit)
        ).all()
        return [
            MessageOut(
                id=r.id,
                session_id=r.session_id,
                role=r.role,
                content=r.content,
                tool_calls=r.tool_calls,
                created_at=r.created_at,
            )
            for r in rows
        ]

    # ---- Notifications (inbox) ----

    @app.post("/notifications", response_model=NotificationOut)
    def create_notification(payload: NotificationIn, s: Session = Depends(get_memory)) -> NotificationOut:
        row = Notification(
            kind=payload.kind,
            title=payload.title,
            body=payload.body,
            project_id=payload.project_id,
            action_id=payload.action_id,
            task_id=payload.task_id,
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return _notification_out(row)

    @app.get("/notifications", response_model=list[NotificationOut])
    def list_notifications(
        unresolved_only: bool = Query(False),
        limit: int = Query(100, le=1000),
        s: Session = Depends(get_memory),
    ) -> list:
        q = select(Notification)
        if unresolved_only:
            q = q.where(Notification.resolved_at.is_(None))
        q = q.order_by(desc(Notification.created_at)).limit(limit)
        return [_notification_out(r) for r in s.scalars(q).all()]

    @app.patch("/notifications/{notif_id}", response_model=NotificationOut)
    def update_notification(
        notif_id: str, payload: NotificationPatch, s: Session = Depends(get_memory)
    ) -> NotificationOut:
        row = s.get(Notification, notif_id)
        if row is None:
            raise HTTPException(404, "notification not found")
        if payload.read is True and row.read_at is None:
            row.read_at = _utcnow()
        if payload.resolution is not None:
            row.resolution = payload.resolution
            row.resolved_at = _utcnow()
        s.commit()
        s.refresh(row)
        return _notification_out(row)

    # ---- Recall (SQL-backed for now; LanceDB swaps in later) ----

    @app.get("/recall", response_model=RecallResponse)
    def recall(
        query: str = Query(..., min_length=1),
        project_id: Optional[str] = Query(None),
        k: int = Query(10, le=100),
        sources: Optional[str] = Query(None, description="comma-separated subset of: action_summary,decision,notification"),
        mem: Session = Depends(get_memory),
        reg: Session = Depends(get_registry),
    ) -> RecallResponse:
        wanted = set((sources or "action_summary,decision,notification").split(","))
        like = f"%{query}%"
        hits: list[RecallHit] = []

        if "action_summary" in wanted:
            aq = select(Action).where(Action.summary.is_not(None)).where(Action.summary.like(like))
            if project_id:
                aq = aq.where(Action.project_id == project_id)
            aq = aq.order_by(desc(Action.started_at)).limit(k)
            for r in reg.scalars(aq).all():
                hits.append(
                    RecallHit(
                        source="action_summary",
                        id=r.id,
                        project_id=r.project_id,
                        text=r.summary or "",
                        created_at=r.started_at,
                    )
                )

        if "decision" in wanted:
            dq = select(Decision).where(Decision.text.like(like))
            if project_id:
                dq = dq.where(Decision.project_id == project_id)
            dq = dq.order_by(desc(Decision.created_at)).limit(k)
            for r in mem.scalars(dq).all():
                hits.append(
                    RecallHit(
                        source="decision",
                        id=r.id,
                        project_id=r.project_id,
                        text=r.text,
                        created_at=r.created_at,
                    )
                )

        if "notification" in wanted:
            nq = select(Notification).where(
                or_(Notification.title.like(like), Notification.body.like(like))
            )
            if project_id:
                nq = nq.where(Notification.project_id == project_id)
            nq = nq.order_by(desc(Notification.created_at)).limit(k)
            for r in mem.scalars(nq).all():
                hits.append(
                    RecallHit(
                        source="notification",
                        id=r.id,
                        project_id=r.project_id,
                        text=f"{r.title}\n{r.body or ''}".strip(),
                        created_at=r.created_at,
                    )
                )

        hits.sort(key=lambda h: h.created_at, reverse=True)
        return RecallResponse(hits=hits[:k])


def _notification_out(row: Notification) -> NotificationOut:
    return NotificationOut(
        id=row.id,
        kind=row.kind,
        title=row.title,
        body=row.body,
        project_id=row.project_id,
        action_id=row.action_id,
        task_id=row.task_id,
        created_at=row.created_at,
        read_at=row.read_at,
        resolved_at=row.resolved_at,
        resolution=row.resolution,
    )
