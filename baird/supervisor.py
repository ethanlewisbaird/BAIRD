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
from .config import load_host_config, load_hub_config


def _pid_file() -> Path:
    return paths.baird_home() / "hub.pid"


def _log_file() -> Path:
    return paths.baird_home() / "hub.log"


def _hub_url() -> str:
    """The URL we probe. host.yaml wins (it's the source of truth for where the
    hub is); if missing, fall back to config.yaml's listen address."""
    try:
        host_cfg = load_host_config()
        return host_cfg.hub_url.rstrip("/")
    except Exception:
        cfg = load_hub_config()
        host, port = cfg.listen.split(":")
        probe_host = "127.0.0.1" if host in ("0.0.0.0", "127.0.0.1") else host
        return f"http://{probe_host}:{port}"


def _hub_is_local() -> bool:
    """True if the hub URL points at this machine — only then is auto-spawn safe."""
    from urllib.parse import urlparse

    host = urlparse(_hub_url()).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def is_hub_running(timeout: float = 0.5) -> bool:
    try:
        r = httpx.get(f"{_hub_url()}/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def ensure_hub_running(*, wait_s: float = 8.0, quiet: bool = False) -> None:
    """Block until the hub answers /health. Spawn one in the background if needed.

    Only auto-spawns when the configured hub_url is local to this machine.
    On a satellite (remote hub_url), fails fast with a clear error if the
    hub isn't reachable — we shouldn't start a hub on the wrong machine.
    """
    if is_hub_running():
        return
    if not _hub_is_local():
        raise RuntimeError(
            f"hub at {_hub_url()} is not reachable — start it on the hub machine "
            "(`baird up` there), or fix `hub_url` in this machine's host.yaml"
        )

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
    """Kill the supervised hub. Returns True if a process was signalled.

    Falls back to a pattern-based pkill when no PID file is present, so that
    a manually-launched `baird hub serve` (the pre-supervisor pattern) can
    still be stopped by `baird stop` / `baird restart`.
    """
    return _stop_by_pid_file(_pid_file()) or _pkill_pattern("baird.cli hub serve") \
        or _pkill_pattern("/bin/baird hub serve")


# ---- daemon (watchdog + executor) -------------------------------------


def _daemon_pid_file() -> Path:
    return paths.baird_home() / "daemon.pid"


def _daemon_log_file() -> Path:
    return paths.baird_home() / "daemon.log"


def is_daemon_running() -> bool:
    """True if a daemon process is alive per its PID file. There's no health
    endpoint to probe, so we rely on `kill -0`."""
    pf = _daemon_pid_file()
    if not pf.exists():
        return False
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError, PermissionError):
        return False
    return True


def ensure_daemon_running(*, quiet: bool = False) -> None:
    """Spawn the satellite-side daemon in the background if not already up.

    No-op when a daemon is already alive. Detaches via `start_new_session`
    so it survives our exit; PID lands in `<baird_home>/daemon.pid`, stdout
    + stderr in `<baird_home>/daemon.log`.
    """
    if is_daemon_running():
        return
    paths.baird_home().mkdir(parents=True, exist_ok=True)
    log = open(_daemon_log_file(), "ab")
    if not quiet:
        print("starting BAIRD daemon in background…", file=sys.stderr)

    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, "-m", "baird.cli", "daemon"],
        stdout=log,
        stdin=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    _daemon_pid_file().write_text(str(proc.pid))


def stop_daemon() -> bool:
    """Kill the supervised daemon. Returns True if a process was signalled.

    Falls back to a pattern-based pkill when no PID file is present, so a
    manually-launched `baird daemon` can still be stopped."""
    return _stop_by_pid_file(_daemon_pid_file()) or _pkill_pattern("baird.cli daemon") \
        or _pkill_pattern("/bin/baird daemon")


def _pkill_pattern(pattern: str) -> bool:
    """Send SIGTERM to processes whose full command line contains `pattern`.

    Returns True if at least one process was signalled. Used as a fallback
    when the PID file is missing — covers daemons started outside of
    `baird up`. Skips the current process so we don't shoot ourselves."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    self_pid = os.getpid()
    killed = False
    for line in out.stdout.split():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid == self_pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed = True
        except (ProcessLookupError, PermissionError):
            continue
    return killed


def _stop_by_pid_file(pf: Path) -> bool:
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
