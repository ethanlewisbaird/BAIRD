"""Tests for the diff_apply module — apply/refuse-dirty/undo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from baird.diff_apply import (
    DiffApplyError,
    apply_diff_to_repo,
    is_baird_commit,
    undo_last_baird_commit,
)


def _git(repo: Path, *args: str, input_text: str | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, input=input_text, check=True
    )
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.test")
    _git(r, "config", "user.name", "tester")
    (r / "a.txt").write_text("hello\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _diff(old: str, new: str, path: str = "a.txt") -> str:
    # Construct a unified diff for a single-file replacement.
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    import difflib

    return "".join(
        difflib.unified_diff(a, b, fromfile=f"a/{path}", tofile=f"b/{path}")
    )


def test_apply_simple_diff(repo: Path) -> None:
    diff = _diff("hello\n", "world\n")
    result = apply_diff_to_repo(repo=repo, diff_text=diff, commit_message="rewrite", action_id="A1")
    assert (repo / "a.txt").read_text() == "world\n"
    assert "a.txt" in result.files_changed
    assert len(result.commit_sha) == 40
    assert is_baird_commit(repo)


def test_apply_refuses_if_target_dirty(repo: Path) -> None:
    (repo / "a.txt").write_text("local edit\n")
    diff = _diff("hello\n", "world\n")
    with pytest.raises(DiffApplyError, match="uncommitted changes"):
        apply_diff_to_repo(repo=repo, diff_text=diff, commit_message="x")


def test_apply_allows_unrelated_dirty(repo: Path) -> None:
    (repo / "other.txt").write_text("scratch\n")
    diff = _diff("hello\n", "world\n")
    apply_diff_to_repo(repo=repo, diff_text=diff, commit_message="x")
    # The unrelated file is still dirty (untracked) — that's fine.
    assert (repo / "other.txt").read_text() == "scratch\n"


def test_apply_rejects_when_strict_dirty(repo: Path) -> None:
    (repo / "untracked.txt").write_text("x")
    _git(repo, "add", "untracked.txt")
    diff = _diff("hello\n", "world\n")
    with pytest.raises(DiffApplyError, match="working tree is dirty"):
        apply_diff_to_repo(
            repo=repo, diff_text=diff, commit_message="x", allow_dirty_outside_targets=False
        )


def test_undo_reverts_only_baird_commits(repo: Path) -> None:
    diff = _diff("hello\n", "world\n")
    apply_diff_to_repo(repo=repo, diff_text=diff, commit_message="rewrite", action_id="A2")
    assert (repo / "a.txt").read_text() == "world\n"

    new_head = undo_last_baird_commit(repo)
    assert len(new_head) == 40
    assert (repo / "a.txt").read_text() == "hello\n"


def test_undo_refuses_non_baird_head(repo: Path) -> None:
    (repo / "b.txt").write_text("y\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "human commit, not baird")
    with pytest.raises(DiffApplyError, match="not a BAIRD commit"):
        undo_last_baird_commit(repo)


def test_garbage_diff_rejected(repo: Path) -> None:
    with pytest.raises(DiffApplyError, match="git apply failed"):
        apply_diff_to_repo(repo=repo, diff_text="not a diff\n", commit_message="x")
