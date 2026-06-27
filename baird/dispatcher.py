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

import subprocess
import time
from pathlib import Path
from typing import Any

from .executor_client import ExecutorClient
from .memory_client import HubClient
from .satellite import load_registry
from .tasks import Task


class DispatcherError(RuntimeError):
    pass


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
        return ex.run_command(
            cmd,
            cwd=cwd,
            project_root=project_root,
            timeout_s=timeout_s,
        )


def run_command_task(
    task: Task,
    *,
    hub: HubClient,
    hub_host_id: str | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Run a `kind=command` task: locally if host_id is None / matches the hub,
    otherwise through the named satellite's executor.

    Records one Action on the hub with the full command + exit code + stdout
    head as summary.
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
            raise
        runtime = time.monotonic() - started
        head = (result.get("stdout") or "")[:600].strip()
        action.set_summary(
            f"command exit={result['exit_code']} runtime={runtime:.1f}s\n{head}"
        )
        action.set_exit_code(result["exit_code"])

    return {
        "action_id": action.id,
        "runtime_s": runtime,
        **result,
    }
