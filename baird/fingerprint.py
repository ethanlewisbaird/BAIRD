"""Fast file-identity fingerprint.

Per the Phase 1 design: every file gets `(size, mtime_ns, head_hash, tail_hash)`
recorded immediately on write — sha256 of the first and last 4 MB. The full sha256
is computed lazily by a background worker; the fingerprint is the cheap identity
key that's always present.

Two files are considered "the same" when:
- both have a computed sha256 and they match, OR
- one or both lack a computed sha256 and all four fingerprint fields match.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

HEAD_TAIL_BYTES = 4 * 1024 * 1024  # 4 MB


@dataclass(frozen=True)
class Fingerprint:
    size: int
    mtime_ns: int
    head_hash: str
    tail_hash: str

    def matches(self, other: "Fingerprint") -> bool:
        return (
            self.size == other.size
            and self.mtime_ns == other.mtime_ns
            and self.head_hash == other.head_hash
            and self.tail_hash == other.tail_hash
        )


def fingerprint(path: str | os.PathLike[str]) -> Fingerprint:
    """Compute the fast fingerprint for a file.

    For files smaller than `HEAD_TAIL_BYTES`, `head_hash` and `tail_hash` cover
    the entire file (and are therefore equal). For files between `HEAD_TAIL_BYTES`
    and `2 * HEAD_TAIL_BYTES`, the head and tail windows overlap; that's fine —
    we still get a stable identity.
    """
    p = Path(path)
    st = p.stat()
    size = st.st_size
    mtime_ns = st.st_mtime_ns

    with open(p, "rb") as f:
        head = f.read(HEAD_TAIL_BYTES)
        head_hash = hashlib.sha256(head).hexdigest()

        if size <= HEAD_TAIL_BYTES:
            tail_hash = head_hash
        else:
            f.seek(max(0, size - HEAD_TAIL_BYTES))
            tail = f.read(HEAD_TAIL_BYTES)
            tail_hash = hashlib.sha256(tail).hexdigest()

    return Fingerprint(
        size=size,
        mtime_ns=mtime_ns,
        head_hash=head_hash,
        tail_hash=tail_hash,
    )


def full_sha256(path: str | os.PathLike[str], chunk_size: int = 1024 * 1024) -> str:
    """Compute the full sha256 of a file. Used by the lazy background worker."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
