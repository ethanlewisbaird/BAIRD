"""Apply a unified diff to a git repo as a single commit.

Phase 3 design, sub-decision #3:

- Refuse to apply if any *target file* in the diff has uncommitted changes.
- Allow dirty changes elsewhere in the tree (by default).
- Single git commit with the supplied message; not auto-pushed.
- `undo_last_baird_commit(repo)` reverts the last commit authored by BAIRD —
  identified by a trailer on the commit message.

The diff text is the same format `git apply` accepts. We don't try to parse it
in Python ourselves — we hand it to `git apply` and trust git.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


BAIRD_COMMIT_TRAILER = "Baird-Action-Id"


class DiffApplyError(RuntimeError):
    pass


@dataclass
class ApplyResult:
    commit_sha: str
    files_changed: list[str]


def _git(repo: Path, *args: str, input_text: str | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        input=input_text,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _files_in_diff(diff_text: str) -> list[str]:
    """Extract the set of target files mentioned in the diff.

    Looks for lines like:
        diff --git a/foo/bar.py b/foo/bar.py
        +++ b/foo/bar.py
    """
    files: list[str] = []
    for line in diff_text.splitlines():
        m = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
        if m:
            files.append(m.group(2))
        elif line.startswith("+++ b/"):
            f = line[len("+++ b/"):].strip()
            if f and f != "/dev/null" and f not in files:
                files.append(f)
    return files


def _dirty_files(repo: Path) -> set[str]:
    code, out, err = _git(repo, "status", "--porcelain")
    if code != 0:
        raise DiffApplyError(f"git status failed: {err.strip()}")
    dirty: set[str] = set()
    for line in out.splitlines():
        if len(line) < 4:
            continue
        # Porcelain v1: XY <space> path  (rename: orig -> new)
        path_part = line[3:]
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        dirty.add(path_part.strip())
    return dirty


def apply_diff_to_repo(
    *,
    repo: Path,
    diff_text: str,
    commit_message: str,
    allow_dirty_outside_targets: bool = True,
    action_id: str | None = None,
) -> ApplyResult:
    """Apply `diff_text` to `repo` and create a single commit.

    Raises DiffApplyError if any file targeted by the diff has uncommitted
    changes (regardless of `allow_dirty_outside_targets`), or if `git apply`
    refuses the patch.
    """
    repo = repo.expanduser().resolve()
    if not (repo / ".git").exists():
        raise DiffApplyError(f"{repo} is not a git repository")

    targets = _files_in_diff(diff_text)
    dirty = _dirty_files(repo)
    target_dirty = sorted(set(targets) & dirty)
    if target_dirty:
        raise DiffApplyError(
            f"refusing to apply — target file(s) have uncommitted changes: {target_dirty}"
        )
    if not allow_dirty_outside_targets and dirty:
        raise DiffApplyError(f"working tree is dirty: {sorted(dirty)}")

    # `git apply --index` stages the changes too, so we can commit directly.
    code, _, err = _git(repo, "apply", "--index", "-", input_text=diff_text)
    if code != 0:
        raise DiffApplyError(f"git apply failed: {err.strip()}")

    full_msg = commit_message
    if action_id:
        full_msg = f"{commit_message}\n\n{BAIRD_COMMIT_TRAILER}: {action_id}\n"
    code, _, err = _git(repo, "commit", "-m", full_msg)
    if code != 0:
        raise DiffApplyError(f"git commit failed: {err.strip()}")

    code, sha, err = _git(repo, "rev-parse", "HEAD")
    if code != 0:
        raise DiffApplyError(f"git rev-parse failed: {err.strip()}")

    code, name_status, _ = _git(repo, "show", "--name-only", "--pretty=", "HEAD")
    files_changed = [line for line in name_status.splitlines() if line]
    return ApplyResult(commit_sha=sha.strip(), files_changed=files_changed)


def is_baird_commit(repo: Path, ref: str = "HEAD") -> bool:
    code, msg, _ = _git(repo, "log", "-1", "--format=%B", ref)
    return code == 0 and BAIRD_COMMIT_TRAILER in msg


def undo_last_baird_commit(repo: Path) -> str:
    """Revert the most recent commit if it was authored by BAIRD. Returns the
    new HEAD sha after the revert. Raises if HEAD is not a BAIRD commit.

    Uses `git revert --no-edit` so the inverse change is itself a commit — keeps
    the registry timeline coherent (no rewrites)."""
    repo = repo.expanduser().resolve()
    if not is_baird_commit(repo):
        raise DiffApplyError("HEAD is not a BAIRD commit — refusing to revert")
    code, _, err = _git(repo, "revert", "--no-edit", "HEAD")
    if code != 0:
        raise DiffApplyError(f"git revert failed: {err.strip()}")
    code, sha, _ = _git(repo, "rev-parse", "HEAD")
    return sha.strip()
