"""Local embedding model loader.

Wraps sentence-transformers in a small singleton so the model loads once per
hub-process lifetime. Configurable via `~/.baird/config.yaml`:

    embedder_model: BAAI/bge-small-en-v1.5     # default
    embedder_device: cpu                       # cpu | cuda | mps

Falls back to CPU if CUDA is requested but unavailable.

This module is gated behind the optional `recall` install — the heavy
sentence-transformers / torch deps only land when you ask for them. Hub
startup tolerates the import failing: /recall just falls back to SQL-only.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

log = logging.getLogger(__name__)


_lock = threading.Lock()
_model = None
_model_name: Optional[str] = None
_dim: Optional[int] = None


DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DEVICE = "cpu"


def _resolve_device(requested: str) -> str:
    if requested in ("cpu", ""):
        return "cpu"
    if requested == "cuda":
        try:
            import torch  # noqa: F401
            if not torch.cuda.is_available():
                log.warning("embedder: cuda requested but not available; falling back to cpu")
                return "cpu"
        except Exception:
            return "cpu"
        return "cuda"
    if requested == "mps":
        try:
            import torch
            if not torch.backends.mps.is_available():
                return "cpu"
        except Exception:
            return "cpu"
        return "mps"
    return requested


def get_model(
    *, model_name: str = DEFAULT_MODEL, device: str = DEFAULT_DEVICE
):
    """Load (once) and return the SentenceTransformer model.

    Re-calls with the same model_name return the cached instance. A
    different model_name triggers a reload.
    """
    global _model, _model_name, _dim
    with _lock:
        if _model is not None and _model_name == model_name:
            return _model
        from sentence_transformers import SentenceTransformer

        log.info("loading embedder %s on %s", model_name, device)
        _model = SentenceTransformer(model_name, device=_resolve_device(device))
        _model_name = model_name
        if hasattr(_model, "get_embedding_dimension"):
            _dim = int(_model.get_embedding_dimension())
        else:
            _dim = int(_model.get_sentence_embedding_dimension())
        return _model


def dim() -> int:
    """Embedding dimensionality. Returns 0 if no model loaded yet."""
    return _dim or 0


def encode(texts: list[str], **kwargs) -> list[list[float]]:
    """Encode a batch. Falls back to an empty list if the model isn't loaded
    (caller should handle that — typically by skipping vector path)."""
    if _model is None:
        return []
    embs = _model.encode(texts, show_progress_bar=False, **kwargs)
    return [list(map(float, e)) for e in embs]


def reset_for_tests() -> None:
    """Drop the cached model. Test fixtures use this when they monkeypatch."""
    global _model, _model_name, _dim
    with _lock:
        _model = None
        _model_name = None
        _dim = None
