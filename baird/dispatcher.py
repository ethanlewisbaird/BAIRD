"""Where a task runs.

A scheduled `Runnable` with `kind="command"` and `host_id="hibu"` should run
the shell command on hibu, not on the hub. This module maps the host_id to a
concrete ExecutorClient (using the hub-side satellites.json registry the
satellite-enroll command writes), or falls back to local subprocess for the
hub itself.

The dispatcher records ONE Action regardless of where execution happens, so
the unified ledger stays unified.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .executor_client import ExecutorClient
from .memory_client import HubClient
from .satellite import load_registry
from .tasks import Task


log = logging.getLogger(__name__)


class DispatcherError(RuntimeError):
    pass


# Network errors worth retrying — SSH tunnels can be briefly unavailable
# during reconnect; httpx surfaces those as ConnectError / ReadTimeout.
_RETRYABLE = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.TransportError,
)


def _retry(
    op: Callable[[], Any],
    *,
    attempts: int = 3,
    backoff_s: tuple[float, ...] = (1.0, 3.0),
    label: str = "",
) -> Any:
    """Run `op`; on retryable network errors, sleep + retry up to `attempts`."""
    for i in range(attempts):
        try:
            return op()
        except _RETRYABLE as e:
            remaining = attempts - i - 1
            if remaining == 0:
                raise
            wait = backoff_s[min(i, len(backoff_s) - 1)]
            log.warning(
                "dispatcher: %s failed (%s); retrying in %.1fs (%d left)",
                label or "operation", e.__class__.__name__, wait, remaining,
            )
            time.sleep(wait)


def _local_run(
    cmd: str, *, cwd: str | None, timeout_s: float
) -> dict[str, Any]:
    proc = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout_s,
    )
    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "tier": "local",
    }


def _satellite_run(
    *,
    host_id: str,
    cmd: str,
    cwd: str | None,
    project_root: str | None,
    timeout_s: float,
) -> dict[str, Any]:
    reg = load_registry()
    entry = reg.get(host_id)
    if entry is None:
        raise DispatcherError(f"host_id {host_id} not in satellites.json — enrol it first")
    port = entry.get("local_fwd_port")
    token = entry.get("executor_auth_token")
    if port is None or token is None:
        raise DispatcherError(
            f"host_id {host_id} registry entry missing port or executor token; "
            "re-enrol to repopulate"
        )
    with ExecutorClient(f"http://127.0.0.1:{port}", token, timeout=timeout_s + 5.0) as ex:
        return _retry(
            lambda: ex.run_command(
                cmd,
                cwd=cwd,
                project_root=project_root,
                timeout_s=timeout_s,
            ),
            label=f"run_command on {host_id}",
        )


def run_command_task(
    task: Task,
    *,
    hub: HubClient,
    hub_host_id: str | None = None,
    project_root: Path | None = None,
    notifier: "Any | None" = None,  # Notifier; lazy import to avoid cycles
) -> dict[str, Any]:
    """Run a `kind=command` task: locally if host_id is None / matches the hub,
    otherwise through the named satellite's executor.

    Records one Action on the hub. If `notifier` is supplied, posts a
    `failure` row when the run fails (status fan-in across hosts), tagged
    with the originating task_id so it lands on `baird inbox` next to other
    failures from the same task.
    """
    runnable = task.runnable
    cmd = runnable.args.get("cmd")
    if not cmd:
        raise DispatcherError("command task needs runnable.args.cmd")
    cwd = runnable.args.get("cwd")
    timeout_s = float(runnable.args.get("timeout_s", 60.0))

    target_host = runnable.host_id
    is_local = (
        target_host is None
        or hub_host_id is not None and target_host == hub_host_id
    )

    started = time.monotonic()
    with hub.start_action(
        project_id=runnable.project_id,
        tool_name="command",
        command=cmd,
        host=target_host or hub_host_id,
        task_id=task.id,
    ) as action:
        try:
            if is_local:
                result = _local_run(cmd, cwd=cwd, timeout_s=timeout_s)
            else:
                result = _satellite_run(
                    host_id=target_host,
                    cmd=cmd,
                    cwd=cwd,
                    project_root=str(project_root) if project_root else None,
                    timeout_s=timeout_s,
                )
        except Exception as e:
            action.set_summary(f"dispatch error: {e}")
            action.set_exit_code(1)
            if notifier is not None:
                notifier.notify(
                    kind="failure",
                    title=f"task {task.id} failed on {target_host or 'hub'}",
                    body=f"dispatch error: {e}",
                    project_id=runnable.project_id,
                    action_id=action.id,
                    task_id=task.id,
                )
            raise
        runtime = time.monotonic() - started
        head = (result.get("stdout") or "")[:600].strip()
        err_head = (result.get("stderr") or "")[:600].strip()
        action.set_summary(
            f"command exit={result['exit_code']} runtime={runtime:.1f}s\n{head}"
        )
        action.set_exit_code(result["exit_code"])
        if notifier is not None and result["exit_code"] != 0:
            notifier.notify(
                kind="failure",
                title=f"task {task.id} exit={result['exit_code']} on {target_host or 'hub'}",
                body=err_head or head or "(no output)",
                project_id=runnable.project_id,
                action_id=action.id,
                task_id=task.id,
            )

    return {
        "action_id": action.id,
        "runtime_s": runtime,
        **result,
    }


def apply_diff_anywhere(
    *,
    diff: str,
    commit_message: str,
    project_root: Path,
    host_id: str | None = None,
    hub_host_id: str | None = None,
    hub: HubClient | None = None,
    action_id: str | None = None,
) -> dict[str, Any]:
    """Apply a unified diff to a repo on the chosen host.

    `host_id=None` or matching the hub's id → local `apply_diff_to_repo`.
    Otherwise → the named satellite's executor via `ExecutorClient.apply_diff`.

    Returns `{"commit_sha", "files_changed"}` so callers can record the
    resulting BAIRD-trailered commit regardless of where it landed.
    """
    is_local = (
        host_id is None
        or (hub_host_id is not None and host_id == hub_host_id)
    )
    if is_local:
        from .diff_apply import apply_diff_to_repo

        res = apply_diff_to_repo(
            repo=project_root,
            diff_text=diff,
            commit_message=commit_message,
            action_id=action_id,
        )
        return {"commit_sha": res.commit_sha, "files_changed": list(res.files_changed)}

    reg = load_registry()
    entry = reg.get(host_id) if host_id else None
    if entry is None:
        raise DispatcherError(f"host_id {host_id} not in satellites.json")
    port = entry.get("local_fwd_port")
    token = entry.get("executor_auth_token")
    if port is None or token is None:
        raise DispatcherError(
            f"host_id {host_id} registry entry missing port or executor token"
        )
    with ExecutorClient(f"http://127.0.0.1:{port}", token, timeout=120.0) as ex:
        out = _retry(
            lambda: ex.apply_diff(
                diff,
                project_root=str(project_root),
                commit_message=commit_message,
            ),
            label=f"apply_diff on {host_id}",
        )
    return {
        "commit_sha": out.get("commit_sha"),
        "files_changed": out.get("files_changed", []),
    }
