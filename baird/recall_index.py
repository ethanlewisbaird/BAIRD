"""LanceDB-backed semantic recall index.

One table — `fragments` — at `<baird_home>/lance/`. Each row is a short
indexable snippet (action summary, decision text, inbox row body, message)
with its embedding vector and pointer back to the source.

Public surface:

  - ensure_index(cfg) — idempotent. Opens or creates the table, returns it.
  - upsert_fragment(...) — append a new fragment. Embed lazily.
  - search(query, *, k, project_id) — vector top-k.
  - promote(action_id, *, kind, text, ...) — tier-3 promotion path used by
    `baird flag` / `baird resolve`.

Failures degrade gracefully: missing optional deps → returns None /
empty list, never raises out of `/recall`.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from . import paths
from .config import HubConfig


log = logging.getLogger(__name__)


_FRAGMENTS = "fragments"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _try_import():
    """Lazy import of optional deps. Returns (lancedb, pyarrow) or (None, None)."""
    try:
        import lancedb
        import pyarrow as pa

        return lancedb, pa
    except Exception as e:
        log.info("recall: optional deps not installed (%s); /recall stays SQL-only", e)
        return None, None


def _schema(pa, dim: int):
    return pa.schema([
        pa.field("id", pa.string()),
        pa.field("source", pa.string()),   # action | decision | notification | message | flag | resolve
        pa.field("source_id", pa.string()),
        pa.field("project_id", pa.string()),
        pa.field("text", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), dim)),
        pa.field("created_at", pa.timestamp("us")),
        pa.field("tier", pa.int32()),       # 1=auto, 2=auto-promoted, 3=user-flagged/resolved
        pa.field("metadata", pa.string()),  # JSON blob
    ])


# Embedding dimensions per known model. Lets us create the LanceDB schema
# without loading the (slow) model at hub startup. First actual upsert /
# search triggers the model load.
_KNOWN_DIMS: dict[str, int] = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
    "BAAI/bge-m3": 1024,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
}


class _LazyTable:
    """Wraps the LanceDB table + the config needed to load the embedder when
    we actually need to embed. ensure_index returns one of these; callers pass
    it to upsert_fragment / search which load the model on first call."""

    def __init__(self, table, *, model_name: str, device: str):
        self.table = table
        self.model_name = model_name
        self.device = device

    def encode(self, texts: list[str]) -> list[list[float]]:
        from . import embeddings as emb

        emb.get_model(model_name=self.model_name, device=self.device)
        return emb.encode(texts)


def ensure_index(cfg: HubConfig, *, lance_dir: Path | None = None):
    """Open or create the fragments table. Returns a `_LazyTable` (the
    embedder loads on first encode), or None if recall is disabled or the
    optional deps are missing."""
    if not getattr(cfg, "recall_enabled", False):
        return None
    lancedb, pa = _try_import()
    if lancedb is None:
        return None

    dim = _KNOWN_DIMS.get(cfg.embedder_model)
    if dim is None:
        # Unknown model: pay the one-time cost of loading it to learn the dim.
        from . import embeddings as emb
        try:
            emb.get_model(model_name=cfg.embedder_model, device=cfg.embedder_device)
            dim = emb.dim()
        except Exception as e:
            log.warning("recall: cannot determine embedding dim (%s); /recall stays SQL-only", e)
            return None

    lance_dir = lance_dir or paths.lance_dir_path()
    lance_dir.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(lance_dir))
    schema = _schema(pa, dim)
    try:
        table = db.open_table(_FRAGMENTS)
    except (FileNotFoundError, ValueError):
        table = db.create_table(_FRAGMENTS, schema=schema)
    return _LazyTable(table, model_name=cfg.embedder_model, device=cfg.embedder_device)


def upsert_fragment(
    lazy,
    *,
    source: str,
    source_id: str,
    project_id: str | None,
    text: str,
    tier: int = 1,
    metadata: dict[str, Any] | None = None,
) -> Optional[str]:
    """Embed `text` and add a row. Returns the new fragment id.

    Idempotency: re-upserts of the same (source, source_id) are appended as
    new rows for now. Deduplication is a follow-up — LanceDB's delete-then-
    insert dance is heavier than we need at current volumes.
    """
    if lazy is None:
        return None

    try:
        vec = lazy.encode([text])[0]
    except Exception as e:
        log.warning("recall: embed failed (%s); skipping", e)
        return None
    import json as _json

    row = {
        "id": str(uuid.uuid4()),
        "source": source,
        "source_id": source_id or "",
        "project_id": project_id or "",
        "text": text,
        "vector": vec,
        "created_at": _utcnow(),
        "tier": int(tier),
        "metadata": _json.dumps(metadata or {}),
    }
    try:
        lazy.table.add([row])
    except Exception as e:
        log.warning("recall: lance add failed (%s)", e)
        return None
    return row["id"]


def search(
    lazy,
    *,
    query: str,
    k: int = 10,
    project_id: str | None = None,
    sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Vector top-k. Returns rows as dicts (compatible with /recall's existing
    SQL hits)."""
    if lazy is None:
        return []

    try:
        qvec = lazy.encode([query])[0]
    except Exception as e:
        log.warning("recall: embed query failed (%s)", e)
        return []
    try:
        s = lazy.table.search(qvec).metric("cosine")
        if project_id:
            s = s.where(f"project_id = '{project_id}'", prefilter=True)
        if sources:
            quoted = ",".join(f"'{src}'" for src in sources)
            s = s.where(f"source IN ({quoted})", prefilter=True)
        s = s.limit(k)
        rows = s.to_list()
    except Exception as e:
        log.warning("recall: lance search failed (%s)", e)
        return []
    return rows


def promote_action(
    table,
    *,
    action_id: str,
    text: str,
    project_id: str | None,
    kind: str = "flag",
    metadata: dict[str, Any] | None = None,
) -> Optional[str]:
    """User-flagged tier-3 fragment (`baird flag`) or auto-promoted error→fix
    pair (`baird resolve`)."""
    return upsert_fragment(
        table,
        source=kind,
        source_id=action_id,
        project_id=project_id,
        text=text,
        tier=3,
        metadata=metadata,
    )
