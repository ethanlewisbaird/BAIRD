"""Satellite-side executor — Phase 3 design, Option C (hybrid).

The same daemon process that runs the watchdog (`daemon.py`) also serves these
HTTP endpoints. Co-locating both eliminates the race where a hub-driven SSH
write and a satellite-side filesystem-watch event create duplicate provenance
rows.

Endpoints (all under `/exec/...`):
  - GET  /exec/health         — liveness probe
  - POST /exec/read_file      — read text content of a file
  - POST /exec/write_file     — write text content (tier 2)
  - POST /exec/run_command    — run a command (classified server-side)
  - POST /exec/apply_diff     — apply a unified diff to a project + commit

Auth: bearer token from `~/.baird/host.yaml`. The orchestrator on the hub
must present this token; if `auth_token` is empty the executor refuses all
calls (deny-by-default).

Scoping: every path argument is resolved and must live under one of the
host's `volumes[].mount` paths, otherwise the call returns 403.

Tier enforcement: tier-3 (destructive) calls are *always* rejected at the
executor. The orchestrator side is responsible for prompting the user and
elevating to a tier-2 override before re-issuing. The executor never sees a
human, so it cannot decide.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .config import HostConfig
from .permissions import (
    Decision,
    Policy,
    PolicyOverride,
    Tier,
    classify_command,
    classify_write,
    default_policy,
)


# ---- Auth + scoping -----------------------------------------------------


class _AuthState:
    """Stored on `app.state`."""

    def __init__(self, host_cfg: HostConfig) -> None:
        self.cfg = host_cfg
        self.mounts = [Path(v.mount).expanduser().resolve() for v in host_cfg.volumes]


def _require_token(app: FastAPI, presented: str | None) -> None:
    expected: str | None = app.state.auth.cfg.auth_token
    if not expected:
        raise HTTPException(403, "executor refuses requests when no auth_token is configured")
    if not presented or presented != f"Bearer {expected}":
        raise HTTPException(401, "missing or invalid bearer token")


def _resolve_under_mount(app: FastAPI, raw_path: str) -> Path:
    """Resolve `raw_path` and ensure it lives under one of the host's volume
    mounts. Returns the resolved absolute path. Raises 403 otherwise.

    For writes the path may not exist yet — we check the parent in that case.
    """
    p = Path(raw_path).expanduser()
    try:
        p_resolved = p.resolve(strict=False)
    except OSError as e:
        raise HTTPException(400, f"could not resolve path: {e}") from e

    check_target = p_resolved if p_resolved.exists() else p_resolved.parent
    for mount in app.state.auth.mounts:
        try:
            check_target.relative_to(mount)
            return p_resolved
        except ValueError:
            continue
    raise HTTPException(403, f"path {p_resolved} is outside the host's volume map")


# ---- Schemas ------------------------------------------------------------


class ReadFileIn(BaseModel):
    path: str


class ReadFileOut(BaseModel):
    path: str
    content: str
    size: int


class WriteFileIn(BaseModel):
    path: str
    content: str
    project_root: Optional[str] = None  # required for tier-2 scoping; else destructive
    create_parents: bool = True


class WriteFileOut(BaseModel):
    path: str
    bytes_written: int


class RunCommandIn(BaseModel):
    command: str
    cwd: Optional[str] = None
    timeout_s: float = 30.0
    project_root: Optional[str] = None
    project_overrides: list[dict] = []  # raw list, parsed to PolicyOverride


class RunCommandOut(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    tier: str


class ApplyDiffIn(BaseModel):
    project_root: str
    diff: str  # unified diff text
    commit_message: str
    allow_dirty_outside_targets: bool = True


class ApplyDiffOut(BaseModel):
    commit_sha: str
    files_changed: list[str]


# ---- App factory --------------------------------------------------------


def _bearer_dep(authorization: str | None = Header(default=None)) -> str | None:
    return authorization


def create_executor_app(host_cfg: HostConfig) -> FastAPI:
    app = FastAPI(title="BAIRD executor", version="0.0.1")
    app.state.auth = _AuthState(host_cfg)

    @app.get("/exec/health")
    def health(authorization: str | None = Depends(_bearer_dep)) -> dict:
        _require_token(app, authorization)
        return {
            "status": "ok",
            "host_id": host_cfg.host_id,
            "volumes": [v.id for v in host_cfg.volumes],
        }

    @app.post("/exec/read_file", response_model=ReadFileOut)
    def read_file(payload: ReadFileIn, authorization: str | None = Depends(_bearer_dep)) -> ReadFileOut:
        _require_token(app, authorization)
        target = _resolve_under_mount(app, payload.path)
        if not target.is_file():
            raise HTTPException(404, "not a regular file")
        try:
            content = target.read_text()
        except UnicodeDecodeError as e:
            raise HTTPException(415, "file is not utf-8 text") from e
        return ReadFileOut(path=str(target), content=content, size=target.stat().st_size)

    @app.post("/exec/write_file", response_model=WriteFileOut)
    def write_file(payload: WriteFileIn, authorization: str | None = Depends(_bearer_dep)) -> WriteFileOut:
        _require_token(app, authorization)
        target = _resolve_under_mount(app, payload.path)
        proj_root = Path(payload.project_root).resolve() if payload.project_root else None
        decision = classify_write(target, project_root=proj_root)
        if decision.tier != Tier.PROJECT:
            raise HTTPException(403, f"write rejected ({decision.tier.value}): {decision.reason}")
        if payload.create_parents:
            target.parent.mkdir(parents=True, exist_ok=True)
        n = target.write_text(payload.content)
        return WriteFileOut(path=str(target), bytes_written=n)

    @app.post("/exec/run_command", response_model=RunCommandOut)
    def run_command(payload: RunCommandIn, authorization: str | None = Depends(_bearer_dep)) -> RunCommandOut:
        _require_token(app, authorization)
        overrides = _parse_overrides(payload.project_overrides)
        decision: Decision = classify_command(payload.command, project_overrides=overrides)
        if decision.tier == Tier.DESTRUCTIVE:
            raise HTTPException(403, f"command rejected as destructive: {decision.reason}")
        cwd_path: Path | None = None
        if payload.cwd:
            cwd_path = _resolve_under_mount(app, payload.cwd)
            if not cwd_path.is_dir():
                raise HTTPException(400, "cwd is not a directory")
        try:
            proc = subprocess.run(
                payload.command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(cwd_path) if cwd_path else None,
                timeout=payload.timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            raise HTTPException(504, f"command timed out after {payload.timeout_s}s") from e
        return RunCommandOut(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            tier=decision.tier.value,
        )

    @app.post("/exec/apply_diff", response_model=ApplyDiffOut)
    def apply_diff(payload: ApplyDiffIn, authorization: str | None = Depends(_bearer_dep)) -> ApplyDiffOut:
        _require_token(app, authorization)
        from .diff_apply import apply_diff_to_repo

        proj_root = _resolve_under_mount(app, payload.project_root)
        if not (proj_root / ".git").exists():
            raise HTTPException(400, "project_root is not a git repository")
        result = apply_diff_to_repo(
            repo=proj_root,
            diff_text=payload.diff,
            commit_message=payload.commit_message,
            allow_dirty_outside_targets=payload.allow_dirty_outside_targets,
        )
        return ApplyDiffOut(commit_sha=result.commit_sha, files_changed=list(result.files_changed))

    return app


def _parse_overrides(raw: Iterable[dict]) -> list[PolicyOverride]:
    out: list[PolicyOverride] = []
    for entry in raw or []:
        try:
            out.append(
                PolicyOverride(
                    command_regex=entry["command_regex"],
                    tier=Tier(entry["tier"]),
                    reason=entry.get("reason", "project override"),
                )
            )
        except (KeyError, ValueError):
            continue
    return out


__all__ = ["create_executor_app", "default_policy", "Policy"]
