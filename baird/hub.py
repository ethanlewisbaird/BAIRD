"""FastAPI service on the hub.

Exposes the registry and memory APIs over HTTP. Watchdog daemons and the
harness orchestrator hit this from any host on the Tailnet.

Routing layout: this module owns app construction + the registry (files) routes;
the rest of the surface (`projects`, `decisions`, `actions`, `sessions`,
`notifications`, `recall`) lives in `hub_routes/*` and is wired in here.

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


def get_memory(request: Request) -> Iterator[Session]:
    """FastAPI dependency: yields a memory SQLAlchemy session from app.state."""
    factory = request.app.state.memory_session
    with factory() as s:
        yield s


def create_app(hub_cfg: Optional[HubConfig] = None) -> FastAPI:
    cfg = hub_cfg or load_hub_config()
    registry_engine = create_registry_engine(cfg.registry_db)
    memory_engine = create_memory_engine(cfg.memory_db)

    app = FastAPI(title="BAIRD hub", version="0.0.1")
    app.state.registry_session = make_session_factory(registry_engine)
    app.state.memory_session = make_session_factory(memory_engine)
    app.state.hub_cfg = cfg

    @app.middleware("http")
    async def _require_bearer(request: Request, call_next):
        token = cfg.auth_token
        if token is None or request.url.path == "/health":
            return await call_next(request)
        presented = request.headers.get("authorization", "")
        if presented != f"Bearer {token}":
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=401, content={"detail": "unauthorised"})
        return await call_next(request)

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

    # ---- Phase 2 routes (registered from sibling module) ----
    from . import hub_memory

    hub_memory.register_routes(app)

    # ---- Model proxy (central key, central ledger) ----
    from . import hub_proxy

    hub_proxy.register_routes(app)

    return app


# Default app used by `baird hub serve` (binds to `~/.baird/...` per user config).
app = create_app()
