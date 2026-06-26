"""SQLAlchemy models for both databases.

Per the Phase 2 design: registry and memory live in two separate SQLite files
served by one FastAPI service. They're declared with separate `DeclarativeBase`
classes so the metadata stays distinct and each binds to its own engine.

Cross-domain joins (e.g. "the action that produced this file" → "the conversation
that drove the action") are done at the application layer, not via DB-level FKs.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# =========================================================================
# Registry database
# =========================================================================


class RegistryBase(DeclarativeBase):
    pass


class File(RegistryBase):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    storage_volume: Mapped[str] = mapped_column(String, index=True)
    relative_path: Mapped[str] = mapped_column(String, index=True)

    size: Mapped[int] = mapped_column(Integer)
    mtime_ns: Mapped[int] = mapped_column(Integer)
    head_hash: Mapped[str] = mapped_column(String(64))
    tail_hash: Mapped[str] = mapped_column(String(64))

    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    sha256_status: Mapped[str] = mapped_column(String, default="pending")  # pending|computed|skipped

    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    created_by_action_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)


class Action(RegistryBase):
    __tablename__ = "actions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    parent_action_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("actions.id"), nullable=True, index=True
    )

    tool_name: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_version: Mapped[str | None] = mapped_column(String, nullable=True)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    host: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    conda_env: Mapped[str | None] = mapped_column(String, nullable=True)
    env_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    slurm_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # tier-2 AI summary

    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    children: Mapped[list["Action"]] = relationship(
        "Action", backref="parent", remote_side="Action.id"
    )


class FileAction(RegistryBase):
    """M:N join — files involved in actions, with their role (input / output / log)."""

    __tablename__ = "file_actions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    file_id: Mapped[str] = mapped_column(String, ForeignKey("files.id"), index=True)
    action_id: Mapped[str] = mapped_column(String, ForeignKey("actions.id"), index=True)
    role: Mapped[str] = mapped_column(String)  # input | output | log


# =========================================================================
# Memory database
# =========================================================================


class MemoryBase(DeclarativeBase):
    pass


class Project(MemoryBase):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    github: Mapped[str | None] = mapped_column(String, nullable=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)

    # checkout_hosts / goals / state / data_aliases / rules
    # are stored as JSON for now — schema can split them out later if needed
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Decision(MemoryBase):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String, index=True)
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String)  # "user" | "ai"
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


class Session(MemoryBase):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String)  # "code" | "chat" | "agent"
    task_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    last_active_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


class Message(MemoryBase):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String, ForeignKey("sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String)  # user | assistant | system | tool
    content: Mapped[str] = mapped_column(Text)
    tool_calls: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


class Notification(MemoryBase):
    """Inbox row. Every notification gets one, regardless of channel."""

    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String)  # approval | failure | result | digest | proposal
    title: Mapped[str] = mapped_column(String)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    action_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String, nullable=True)  # accept|reject|...


# =========================================================================
# Engine setup helpers
# =========================================================================


def _make_engine(path: str):
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{p}", future=True)


def _add_missing_columns(engine, metadata) -> None:
    """Lightweight self-migration: for each declared column not present on the
    existing table, ALTER TABLE ADD COLUMN. SQLite supports this for any
    nullable column without a default, which is exactly what BAIRD declares.

    We don't try to handle dropped or renamed columns — those are real
    migrations and warrant a real tool. This handles the common case of
    upgrading across phase boundaries.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    sqlalchemy_to_sqlite = {
        "INTEGER": "INTEGER",
        "BIGINT": "INTEGER",
        "FLOAT": "REAL",
        "REAL": "REAL",
        "VARCHAR": "VARCHAR",
        "TEXT": "TEXT",
        "BOOLEAN": "BOOLEAN",
        "JSON": "JSON",
        "DATETIME": "DATETIME",
    }
    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in present:
                    continue
                col_type = str(col.type).split("(")[0].upper()
                sql_type = sqlalchemy_to_sqlite.get(col_type, col_type)
                conn.execute(text(
                    f'ALTER TABLE "{table.name}" ADD COLUMN '
                    f'"{col.name}" {sql_type}'
                ))


def create_registry_engine(path: str):
    engine = _make_engine(path)
    RegistryBase.metadata.create_all(engine)
    _add_missing_columns(engine, RegistryBase.metadata)
    return engine


def create_memory_engine(path: str):
    engine = _make_engine(path)
    MemoryBase.metadata.create_all(engine)
    _add_missing_columns(engine, MemoryBase.metadata)
    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
