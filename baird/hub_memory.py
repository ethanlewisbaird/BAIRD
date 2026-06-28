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
    task_id: Optional[str] = None
    model_name: Optional[str] = None


class ActionPatch(BaseModel):
    finished_at: Optional[dt.datetime] = None
    exit_code: Optional[int] = None
    summary: Optional[str] = None
    cost_usd: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


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
    task_id: Optional[str] = None
    model_name: Optional[str] = None
    cost_usd: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


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
        task_id=row.task_id,
        model_name=row.model_name,
        cost_usd=row.cost_usd,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
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
    def list_projects(
        parent_id: str | None = None,
        s: Session = Depends(get_memory),
    ) -> list:
        rows = s.scalars(select(Project).order_by(Project.created_at)).all()
        out = [_project_out(r) for r in rows]
        if parent_id is not None:
            out = [p for p in out if (p.config or {}).get("parent_id") == parent_id]
        return out

    @app.get("/projects/{project_id}", response_model=ProjectOut)
    def get_project(project_id: str, s: Session = Depends(get_memory)) -> ProjectOut:
        row = s.get(Project, project_id)
        if row is None:
            raise HTTPException(404, "project not found")
        return _project_out(row)

    @app.get("/projects/{project_id}/related", response_model=list[ProjectOut])
    def list_related_projects(
        project_id: str, s: Session = Depends(get_memory)
    ) -> list:
        """Self + parent + siblings (for a child), or self + children (for a
        parent). Used by sibling-aware recall and the `where` tool."""
        row = s.get(Project, project_id)
        if row is None:
            raise HTTPException(404, "project not found")
        all_rows = s.scalars(select(Project)).all()
        cfg = row.config or {}
        parent_id = cfg.get("parent_id")
        out: list[Project] = [row]
        if parent_id:
            parent = s.get(Project, parent_id)
            if parent is not None:
                out.append(parent)
            for r in all_rows:
                if r.id == row.id:
                    continue
                if (r.config or {}).get("parent_id") == parent_id:
                    out.append(r)
        else:
            for r in all_rows:
                if (r.config or {}).get("parent_id") == row.id:
                    out.append(r)
        # Dedup preserving order.
        seen: set[str] = set()
        deduped: list[Project] = []
        for r in out:
            if r.id in seen:
                continue
            seen.add(r.id)
            deduped.append(r)
        return [_project_out(r) for r in deduped]

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
        _recall_upsert(
            app, source="decision",
            source_id=row.id, project_id=row.project_id, text=row.text,
        )
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
            task_id=payload.task_id,
            model_name=payload.model_name,
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
            # If the caller supplied an exit code but didn't pin a timestamp,
            # mark the action finished now — natural "this is done" semantic.
            if payload.finished_at is None and row.finished_at is None:
                row.finished_at = _utcnow()
        if payload.summary is not None:
            row.summary = payload.summary
        if payload.cost_usd is not None:
            row.cost_usd = payload.cost_usd
        if payload.input_tokens is not None:
            row.input_tokens = payload.input_tokens
        if payload.output_tokens is not None:
            row.output_tokens = payload.output_tokens
        s.commit()
        s.refresh(row)
        if row.summary:
            _recall_upsert(
                app, source="action",
                source_id=row.id, project_id=row.project_id, text=row.summary,
            )
        return _action_out(row)

    @app.get("/actions", response_model=list[ActionOut])
    def list_actions(
        project_id: Optional[str] = Query(None),
        task_id: Optional[str] = Query(None),
        started_after: Optional[dt.datetime] = Query(None),
        unfinished_only: bool = Query(False),
        limit: int = Query(50, le=500),
        s: Session = Depends(get_registry),
    ) -> list:
        q = select(Action)
        if project_id is not None:
            q = q.where(Action.project_id == project_id)
        if task_id is not None:
            q = q.where(Action.task_id == task_id)
        if started_after is not None:
            q = q.where(Action.started_at >= started_after)
        if unfinished_only:
            q = q.where(Action.finished_at.is_(None))
        q = q.order_by(desc(Action.started_at)).limit(limit)
        return [_action_out(r) for r in s.scalars(q).all()]

    @app.get("/stats")
    def stats(
        reg: Session = Depends(get_registry),
        mem: Session = Depends(get_memory),
    ) -> dict:
        from sqlalchemy import func

        files_live = reg.scalar(
            select(func.count(File.id)).where(File.deleted_at.is_(None))
        ) or 0
        actions_total = reg.scalar(select(func.count(Action.id))) or 0
        actions_running = reg.scalar(
            select(func.count(Action.id)).where(Action.finished_at.is_(None))
        ) or 0
        projects_total = mem.scalar(select(func.count(Project.id))) or 0
        decisions_total = mem.scalar(select(func.count(Decision.id))) or 0
        notifications_unresolved = mem.scalar(
            select(func.count(Notification.id)).where(Notification.resolved_at.is_(None))
        ) or 0
        return {
            "files_live": int(files_live),
            "actions_total": int(actions_total),
            "actions_running": int(actions_running),
            "projects": int(projects_total),
            "decisions": int(decisions_total),
            "notifications_unresolved": int(notifications_unresolved),
        }

    @app.get("/budgets/usage")
    def budgets_usage(
        since_hours: int = Query(24, ge=1, le=720),
        task_id: Optional[str] = Query(None),
        s: Session = Depends(get_registry),
    ) -> dict:
        from sqlalchemy import func
        cutoff = _utcnow() - dt.timedelta(hours=since_hours)
        q = select(
            func.coalesce(func.sum(Action.cost_usd), 0.0),
            func.coalesce(func.sum(Action.input_tokens), 0),
            func.coalesce(func.sum(Action.output_tokens), 0),
            func.count(Action.id),
        ).where(Action.started_at >= cutoff)
        if task_id is not None:
            q = q.where(Action.task_id == task_id)
        cost, in_tok, out_tok, n = s.execute(q).one()
        return {
            "since_hours": since_hours,
            "task_id": task_id,
            "cost_usd": float(cost or 0.0),
            "input_tokens": int(in_tok or 0),
            "output_tokens": int(out_tok or 0),
            "actions": int(n or 0),
        }

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

    @app.get("/sessions", response_model=list[SessionOut])
    def list_sessions(
        task_id: Optional[str] = Query(None),
        project_id: Optional[str] = Query(None),
        mode: Optional[str] = Query(None),
        limit: int = Query(20, le=200),
        s: Session = Depends(get_memory),
    ) -> list:
        q = select(SessionRow)
        if task_id is not None:
            q = q.where(SessionRow.task_id == task_id)
        if project_id is not None:
            q = q.where(SessionRow.project_id == project_id)
        if mode is not None:
            q = q.where(SessionRow.mode == mode)
        q = q.order_by(desc(SessionRow.last_active_at)).limit(limit)
        return [
            SessionOut(
                id=r.id, project_id=r.project_id, mode=r.mode, task_id=r.task_id,
                started_at=r.started_at, last_active_at=r.last_active_at,
            )
            for r in s.scalars(q).all()
        ]

    @app.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
    def get_messages(
        session_id: str,
        limit: int = Query(200, le=1000),
        offset: int = Query(0, ge=0),
        s: Session = Depends(get_memory),
    ) -> list:
        if s.get(SessionRow, session_id) is None:
            raise HTTPException(404, "session not found")
        rows = s.scalars(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at)
            .offset(offset)
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
        text = f"{row.title}\n{row.body or ''}".strip()
        if text:
            _recall_upsert(
                app, source="notification",
                source_id=row.id, project_id=row.project_id, text=text,
            )
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

        # Hybrid: merge vector hits on top of SQL hits, dedup by (source, id),
        # then return top-k by (vector score where available, else recency).
        if getattr(app.state, "recall_table", None) is not None:
            from . import recall_index

            # Map the public API's source names → the LanceDB source labels we
            # store under. "action_summary" → "action" (we also stamp "flag"
            # and "resolve" rows with the source_id of an action; treat both
            # as action hits).
            lance_sources: list[str] | None = None
            if wanted:
                mapped: set[str] = set()
                for w in wanted:
                    if w == "action_summary":
                        mapped.update({"action", "flag", "resolve"})
                    else:
                        mapped.add(w)
                lance_sources = sorted(mapped)
            vec_rows = recall_index.search(
                app.state.recall_table,
                query=query,
                k=k * 2,  # over-fetch; we'll dedup + clip below
                project_id=project_id,
                sources=lance_sources,
            )
            seen = {(h.source, h.id) for h in hits}
            for r in vec_rows:
                # LanceDB returns the source-table row fields plus `_distance`.
                src = r.get("source", "")
                sid = r.get("source_id", "")
                # Map LanceDB sources back to the /recall API's source labels.
                api_src = {
                    "action": "action_summary",
                    "decision": "decision",
                    "notification": "notification",
                    "message": "message",
                    "flag": "action_summary",
                    "resolve": "action_summary",
                }.get(src, src)
                if (api_src, sid) in seen:
                    continue
                seen.add((api_src, sid))
                created = r.get("created_at")
                hits.append(
                    RecallHit(
                        source=api_src,
                        id=sid,
                        project_id=r.get("project_id") or None,
                        text=r.get("text", ""),
                        created_at=created if isinstance(created, dt.datetime) else _utcnow(),
                    )
                )

        hits.sort(key=lambda h: h.created_at, reverse=True)
        return RecallResponse(hits=hits[:k])

    @app.post("/recall/flag")
    def recall_flag(payload: dict) -> dict:
        """Promote an action snippet to tier-3 in the recall index."""
        table = getattr(app.state, "recall_table", None)
        if table is None:
            return {"id": None, "reason": "recall not configured"}
        from . import recall_index

        fid = recall_index.promote_action(
            table,
            action_id=payload.get("action_id", ""),
            text=payload.get("text", ""),
            project_id=payload.get("project_id"),
            kind="flag",
            metadata=payload.get("metadata"),
        )
        return {"id": fid}

    @app.post("/recall/resolve")
    def recall_resolve(payload: dict) -> dict:
        """Promote an error→fix pair to tier-3."""
        table = getattr(app.state, "recall_table", None)
        if table is None:
            return {"id": None, "reason": "recall not configured"}
        from . import recall_index

        fid = recall_index.promote_action(
            table,
            action_id=payload.get("error_action_id", ""),
            text=payload.get("text", ""),
            project_id=payload.get("project_id"),
            kind="resolve",
            metadata={"fix_action_id": payload.get("fix_action_id")},
        )
        return {"id": fid}

    _register_event_routes(app)


def _recall_upsert(
    app, *, source: str, source_id: str, project_id: str | None, text: str
) -> None:
    """Best-effort: upsert a fragment into the recall index if it's loaded.
    Failures are logged but never break the calling route."""
    table = getattr(app.state, "recall_table", None)
    if table is None:
        return
    try:
        from . import recall_index

        recall_index.upsert_fragment(
            table,
            source=source,
            source_id=source_id,
            project_id=project_id,
            text=text,
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception("recall upsert failed")


def _register_event_routes(app: FastAPI) -> None:
    from .db import Event

    @app.post("/events/{name}")
    def emit_event(
        name: str,
        payload: dict | None = None,
        s: Session = Depends(get_memory),
    ) -> dict:
        row = Event(name=name, payload=payload)
        s.add(row)
        s.commit()
        return {"id": row.id, "name": row.name, "created_at": row.created_at.isoformat()}

    @app.get("/events")
    def list_events(
        unconsumed_only: bool = False,
        limit: int = 50,
        s: Session = Depends(get_memory),
    ) -> list[dict]:
        q = select(Event).order_by(Event.created_at.desc())
        if unconsumed_only:
            q = q.where(Event.consumed_at.is_(None))
        rows = s.scalars(q.limit(limit)).all()
        return [
            {
                "id": r.id,
                "name": r.name,
                "payload": r.payload,
                "created_at": r.created_at.isoformat(),
                "consumed_at": r.consumed_at.isoformat() if r.consumed_at else None,
            }
            for r in rows
        ]

    @app.post("/events/{event_id}/consume")
    def mark_consumed(event_id: str, s: Session = Depends(get_memory)) -> dict:
        row = s.get(Event, event_id)
        if row is None:
            raise HTTPException(404, "event not found")
        if row.consumed_at is None:
            from datetime import datetime, timezone
            row.consumed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            s.commit()
        return {"id": row.id, "consumed_at": row.consumed_at.isoformat()}


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
