"""FastAPI service on the hub.

Exposes the registry and memory APIs over HTTP. Watchdog daemons and the
harness orchestrator hit this from any host on the Tailnet.

This is a skeleton — only a `/health` route and a stub `POST /files` exist.
The full route surface gets filled in during Phase 1 implementation.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .config import load_hub_config
from .db import (
    File,
    create_memory_engine,
    create_registry_engine,
    make_session_factory,
)

app = FastAPI(title="BAIRD hub", version="0.0.1")

_hub_cfg = load_hub_config()
_registry_engine = create_registry_engine(_hub_cfg.registry_db)
_memory_engine = create_memory_engine(_hub_cfg.memory_db)
_RegistrySession = make_session_factory(_registry_engine)
_MemorySession = make_session_factory(_memory_engine)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---- Files (registry) ---------------------------------------------------


class FileIn(BaseModel):
    storage_volume: str
    relative_path: str
    size: int
    mtime_ns: int
    head_hash: str
    tail_hash: str
    sha256: str | None = None


class FileOut(FileIn):
    id: str
    sha256_status: str


@app.post("/files", response_model=FileOut)
def register_file(file: FileIn) -> FileOut:
    with _RegistrySession() as s:
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


@app.get("/files/{file_id}", response_model=FileOut)
def get_file(file_id: str) -> FileOut:
    with _RegistrySession() as s:
        row = s.get(File, file_id)
        if row is None:
            raise HTTPException(404, "file not found")
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
