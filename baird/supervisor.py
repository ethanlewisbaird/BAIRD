"""Background-process supervisor for the hub.

The hub is a long-lived service, but the user shouldn't have to think about
that. `ensure_hub_running()` probes /health; if the hub isn't up, it spawns
one in the background, writes its PID to `<baird_home>/hub.pid`, points
stdout/stderr at `<baird_home>/hub.log`, then waits for it to answer.

`stop_hub()` reads the PID file and SIGTERMs the process.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

from . import paths
from .config import load_hub_config


def _pid_file() -> Path:
    return paths.baird_home() / "hub.pid"


def _log_file() -> Path:
    return paths.baird_home() / "hub.log"


def _hub_url() -> str:
    cfg = load_hub_config()
    host, port = cfg.listen.split(":")
    # Hub binds to listen-host; we probe via localhost — same machine by design.
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "127.0.0.1") else host
    return f"http://{probe_host}:{port}"


def is_hub_running(timeout: float = 0.5) -> bool:
    try:
        r = httpx.get(f"{_hub_url()}/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def ensure_hub_running(*, wait_s: float = 8.0, quiet: bool = False) -> None:
    """Block until the hub answers /health. Spawn one in the background if needed."""
    if is_hub_running():
        return

    paths.baird_home().mkdir(parents=True, exist_ok=True)
    log = open(_log_file(), "ab")
    if not quiet:
        print("starting BAIRD hub in background…", file=sys.stderr)

    env = os.environ.copy()
    # Child inherits BAIRD_HOME so it reads the same config.
    proc = subprocess.Popen(
        [sys.executable, "-m", "baird.cli", "hub", "serve"],
        stdout=log,
        stdin=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,  # detach: survives our exit
    )
    _pid_file().write_text(str(proc.pid))

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if is_hub_running():
            return
        if proc.poll() is not None:
            raise RuntimeError(
                f"hub exited early (code {proc.returncode}); see {_log_file()}"
            )
        time.sleep(0.15)
    raise RuntimeError(
        f"hub did not answer within {wait_s}s; see {_log_file()}"
    )


def stop_hub() -> bool:
    """Kill the supervised hub. Returns True if a process was signalled."""
    pf = _pid_file()
    if not pf.exists():
        return False
    try:
        pid = int(pf.read_text().strip())
    except ValueError:
        pf.unlink(missing_ok=True)
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pf.unlink(missing_ok=True)
        return False
    pf.unlink(missing_ok=True)
    return True
