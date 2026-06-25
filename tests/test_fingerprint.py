"""Tests for the fast file-identity fingerprint."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from baird.fingerprint import HEAD_TAIL_BYTES, Fingerprint, fingerprint, full_sha256


def _write(p: Path, data: bytes) -> Path:
    p.write_bytes(data)
    return p


def test_empty_file(tmp_path: Path) -> None:
    p = _write(tmp_path / "empty", b"")
    fp = fingerprint(p)

    empty_hash = hashlib.sha256(b"").hexdigest()
    assert fp.size == 0
    assert fp.head_hash == empty_hash
    assert fp.tail_hash == empty_hash


def test_small_file_head_and_tail_equal(tmp_path: Path) -> None:
    """For files <= HEAD_TAIL_BYTES, head_hash and tail_hash should be identical."""
    data = b"hello world" * 100
    p = _write(tmp_path / "small", data)
    fp = fingerprint(p)

    assert fp.size == len(data)
    assert fp.head_hash == fp.tail_hash
    assert fp.head_hash == hashlib.sha256(data).hexdigest()


def test_large_file_head_and_tail_differ(tmp_path: Path) -> None:
    """For files > 2 * HEAD_TAIL_BYTES, head_hash and tail_hash should differ when the content does."""
    head = b"A" * HEAD_TAIL_BYTES
    middle = b"M" * (1024 * 1024)
    tail = b"Z" * HEAD_TAIL_BYTES
    p = _write(tmp_path / "large", head + middle + tail)

    fp = fingerprint(p)
    assert fp.size == 2 * HEAD_TAIL_BYTES + len(middle)
    assert fp.head_hash == hashlib.sha256(head).hexdigest()
    assert fp.tail_hash == hashlib.sha256(tail).hexdigest()
    assert fp.head_hash != fp.tail_hash


def test_matches_same_content(tmp_path: Path) -> None:
    """Two files with same content and stamped with same mtime should have matching fingerprints."""
    data = b"reproducible content" * 50
    a = _write(tmp_path / "a", data)
    b = _write(tmp_path / "b", data)
    # Force identical mtimes
    ts = 1_700_000_000
    os.utime(a, (ts, ts))
    os.utime(b, (ts, ts))

    fa = fingerprint(a)
    fb = fingerprint(b)
    assert fa.matches(fb)


def test_does_not_match_different_content(tmp_path: Path) -> None:
    a = _write(tmp_path / "a", b"content one")
    b = _write(tmp_path / "b", b"content two")
    assert not fingerprint(a).matches(fingerprint(b))


def test_does_not_match_different_size(tmp_path: Path) -> None:
    a = _write(tmp_path / "a", b"short")
    b = _write(tmp_path / "b", b"a longer string")
    assert fingerprint(a).size != fingerprint(b).size
    assert not fingerprint(a).matches(fingerprint(b))


def test_fingerprint_is_frozen(tmp_path: Path) -> None:
    p = _write(tmp_path / "f", b"x")
    fp = fingerprint(p)
    with pytest.raises(Exception):  # FrozenInstanceError
        fp.size = 999  # type: ignore[misc]


def test_full_sha256(tmp_path: Path) -> None:
    data = b"some content for full hashing" * 1000
    p = _write(tmp_path / "f", data)
    assert full_sha256(p) == hashlib.sha256(data).hexdigest()


def test_full_sha256_empty(tmp_path: Path) -> None:
    p = _write(tmp_path / "empty", b"")
    assert full_sha256(p) == hashlib.sha256(b"").hexdigest()


def test_fingerprint_dataclass_fields() -> None:
    """Sanity check the dataclass shape matches the design."""
    f = Fingerprint(size=1, mtime_ns=2, head_hash="a" * 64, tail_hash="b" * 64)
    assert f.size == 1
    assert f.mtime_ns == 2
    assert len(f.head_hash) == 64
    assert len(f.tail_hash) == 64
