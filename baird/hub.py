"""FastAPI service on the hub.

Exposes the registry and memory APIs over HTTP. Watchdog daemons and the
harness orchestrator hit this from any host on the Tailnet.

Phase 1 surface: file dedup-on-write, list with filters, sha256 patching.
The memory routes and the rest of the registry routes land in later phases.

Use `create_app(hub_cfg)` to construct an app bound to a specific config —
useful for tests. The module-level `app` is bound to the default user config.
"""

import datetime as dt
from collections.abc import Iterator
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import HubConfig, load_hub_config
from .db import (
    File,
    create_memory_engine,
    create_registry_engine,
    make_session_factory,
)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---- IO schemas ---------------------------------------------------------


class FileIn(BaseModel):
    storage_volume: str
    relative_path: str
    size: int
    mtime_ns: int
    head_hash: str
    tail_hash: str
    sha256: Optional[str] = None


class FileOut(FileIn):
    id: str
    sha256_status: str


class FilePatch(BaseModel):
    sha256: Optional[str] = None
    sha256_status: Optional[str] = None


def _to_out(row: File) -> FileOut:
    return FileOut(
        id=row.id,
        storage_volume=row.storage_volume,
        relative_path=row.relative_path,
        size=row.size,
        mtime_ns=row.mtime_ns,
        head_hash=row.head_hash,
        tail_hash=row.tail_hash,
        sha256=row.sha256,
        sha256_status=row.sha256_status,
    )


def _fingerprint_matches(row: File, file: FileIn) -> bool:
    return (
        row.size == file.size
        and row.mtime_ns == file.mtime_ns
        and row.head_hash == file.head_hash
        and row.tail_hash == file.tail_hash
    )


# ---- App factory --------------------------------------------------------


def get_registry(request: Request) -> Iterator[Session]:
    """FastAPI dependency: yields a registry SQLAlchemy session from app.state."""
    factory = request.app.state.registry_session
    with factory() as s:
        yield s


def create_app(hub_cfg: Optional[HubConfig] = None) -> FastAPI:
    cfg = hub_cfg or load_hub_config()
    registry_engine = create_registry_engine(cfg.registry_db)
    create_memory_engine(cfg.memory_db)  # ensure the memory DB exists too

    app = FastAPI(title="BAIRD hub", version="0.0.1")
    app.state.registry_session = make_session_factory(registry_engine)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/files", response_model=FileOut)
    def register_file(file: FileIn, s: Session = Depends(get_registry)) -> FileOut:
        existing = s.scalars(
            select(File).where(
                File.storage_volume == file.storage_volume,
                File.relative_path == file.relative_path,
                File.deleted_at.is_(None),
            )
        ).first()

        now = _utcnow()

        if existing and _fingerprint_matches(existing, file):
            existing.last_seen_at = now
            # If the caller supplied a sha256 and we didn't have one, fill it in.
            if file.sha256 and not existing.sha256:
                existing.sha256 = file.sha256
                existing.sha256_status = "computed"
            s.commit()
            s.refresh(existing)
            return _to_out(existing)

        if existing:
            existing.deleted_at = now

        row = File(
            storage_volume=file.storage_volume,
            relative_path=file.relative_path,
            size=file.size,
            mtime_ns=file.mtime_ns,
            head_hash=file.head_hash,
            tail_hash=file.tail_hash,
            sha256=file.sha256,
            sha256_status="computed" if file.sha256 else "pending",
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return _to_out(row)

    @app.get("/files", response_model=list[FileOut])
    def list_files(
        sha256_status: Optional[str] = Query(None),
        storage_volume: Optional[str] = Query(None),
        include_deleted: bool = Query(False),
        limit: int = Query(100, le=1000),
        s: Session = Depends(get_registry),
    ) -> list:
        q = select(File)
        if not include_deleted:
            q = q.where(File.deleted_at.is_(None))
        if sha256_status:
            q = q.where(File.sha256_status == sha256_status)
        if storage_volume:
            q = q.where(File.storage_volume == storage_volume)
        q = q.limit(limit)
        return [_to_out(r) for r in s.scalars(q).all()]

    @app.get("/files/{file_id}", response_model=FileOut)
    def get_file(file_id: str, s: Session = Depends(get_registry)) -> FileOut:
        row = s.get(File, file_id)
        if row is None:
            raise HTTPException(404, "file not found")
        return _to_out(row)

    @app.patch("/files/{file_id}", response_model=FileOut)
    def update_file(file_id: str, patch: FilePatch, s: Session = Depends(get_registry)) -> FileOut:
        row = s.get(File, file_id)
        if row is None:
            raise HTTPException(404, "file not found")
        if patch.sha256 is not None:
            row.sha256 = patch.sha256
        if patch.sha256_status is not None:
            row.sha256_status = patch.sha256_status
        s.commit()
        s.refresh(row)
        return _to_out(row)

    return app


# Default app used by `baird hub serve` (binds to `~/.baird/...` per user config).
app = create_app()
