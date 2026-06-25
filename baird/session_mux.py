"""Session multiplexer abstraction — Phase 4 cross-cutting requirement.

Long-running harness commands run inside a named tmux or screen session on
the satellite, so SSH disconnects don't kill them and the user can `attach`
later to watch live. Session name is deterministic: `baird-<task_or_project>-<short_id>`.

Per `host.yaml` → `session_multiplexer`:
  - `auto`   : prefer tmux, fall back to screen, fall back to `none`
  - `tmux`   : require tmux, error if missing
  - `screen` : require screen, error if missing
  - `none`   : run with `nohup` + logfile; output picked up by watchdog

All backends expose the same `Multiplexer` protocol so callers don't branch.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


def deterministic_session_name(*, prefix: str, scope: str, action_id: str) -> str:
    """Stable name for `(scope, action_id)` — short enough for tmux/screen, unique enough."""
    return f"{prefix}-{scope}-{action_id[:8]}"


@dataclass
class SessionInfo:
    name: str
    backend: str  # "tmux" | "screen" | "none"
    pid: int | None = None
    extra: dict[str, str] | None = None


class Multiplexer(Protocol):
    backend: str

    def create_session(self, *, name: str, cwd: str | None = None, env: dict[str, str] | None = None) -> SessionInfo: ...
    def send(self, *, name: str, command: str) -> None: ...
    def attach_cmd(self, *, name: str) -> list[str]: ...
    def list_sessions(self) -> list[SessionInfo]: ...
    def kill(self, *, name: str) -> bool: ...


class MultiplexerError(RuntimeError):
    pass


# ---- tmux --------------------------------------------------------------


class TmuxBackend:
    backend = "tmux"

    def _tmux(self, *args: str) -> tuple[int, str, str]:
        proc = subprocess.run(["tmux", *args], capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr

    def create_session(self, *, name: str, cwd: str | None = None, env: dict[str, str] | None = None) -> SessionInfo:
        args = ["new-session", "-d", "-s", name]
        if cwd:
            args.extend(["-c", cwd])
        if env:
            for k, v in env.items():
                args.extend(["-e", f"{k}={v}"])
        code, _, err = self._tmux(*args)
        if code != 0:
            raise MultiplexerError(f"tmux new-session failed: {err.strip()}")
        return SessionInfo(name=name, backend=self.backend)

    def send(self, *, name: str, command: str) -> None:
        code, _, err = self._tmux("send-keys", "-t", name, command, "Enter")
        if code != 0:
            raise MultiplexerError(f"tmux send-keys failed: {err.strip()}")

    def attach_cmd(self, *, name: str) -> list[str]:
        return ["tmux", "attach", "-t", name]

    def list_sessions(self) -> list[SessionInfo]:
        code, out, _ = self._tmux("list-sessions", "-F", "#S")
        if code != 0:
            return []
        return [SessionInfo(name=line.strip(), backend=self.backend) for line in out.splitlines() if line.strip()]

    def kill(self, *, name: str) -> bool:
        code, _, _ = self._tmux("kill-session", "-t", name)
        return code == 0


# ---- screen ------------------------------------------------------------


class ScreenBackend:
    backend = "screen"

    def _screen(self, *args: str) -> tuple[int, str, str]:
        proc = subprocess.run(["screen", *args], capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr

    def create_session(self, *, name: str, cwd: str | None = None, env: dict[str, str] | None = None) -> SessionInfo:
        # `-dmS name` = detached, multi-attach, session name.
        args = ["-dmS", name, "bash"]
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)
        proc = subprocess.run(
            ["screen", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            env=proc_env,
        )
        if proc.returncode != 0:
            raise MultiplexerError(f"screen -dmS failed: {proc.stderr.strip()}")
        return SessionInfo(name=name, backend=self.backend)

    def send(self, *, name: str, command: str) -> None:
        # `-X stuff` requires the trailing newline as literal \n.
        code, _, err = self._screen("-S", name, "-X", "stuff", f"{command}\n")
        if code != 0:
            raise MultiplexerError(f"screen stuff failed: {err.strip()}")

    def attach_cmd(self, *, name: str) -> list[str]:
        return ["screen", "-r", name]

    def list_sessions(self) -> list[SessionInfo]:
        code, out, _ = self._screen("-list")
        if code != 0 and "No Sockets" not in (out or ""):
            return []
        sessions: list[SessionInfo] = []
        for line in (out or "").splitlines():
            line = line.strip()
            # Format: "\t12345.session-name\t(Detached)"
            if "." in line and ("Detached" in line or "Attached" in line):
                token = line.split()[0]
                pid_str, _, name = token.partition(".")
                try:
                    pid = int(pid_str)
                except ValueError:
                    pid = None
                sessions.append(SessionInfo(name=name, backend=self.backend, pid=pid))
        return sessions

    def kill(self, *, name: str) -> bool:
        code, _, _ = self._screen("-S", name, "-X", "quit")
        return code == 0


# ---- noop --------------------------------------------------------------


class NoopBackend:
    """`session_multiplexer: none` — runs commands inline via subprocess. No
    long-lived sessions; `attach` is meaningless. Provided so the rest of the
    code path doesn't need to special-case missing multiplexers."""

    backend = "none"

    def create_session(self, **_: object) -> SessionInfo:
        return SessionInfo(name="(noop)", backend=self.backend)

    def send(self, *, name: str, command: str) -> None:
        subprocess.run(command, shell=True, check=False)

    def attach_cmd(self, *, name: str) -> list[str]:
        return ["true"]

    def list_sessions(self) -> list[SessionInfo]:
        return []

    def kill(self, *, name: str) -> bool:
        return True


# ---- factory -----------------------------------------------------------


def select_backend(preference: str = "auto") -> Multiplexer:
    """Pick a Multiplexer based on `host.yaml` → `session_multiplexer`.

    `preference` ∈ {"auto", "tmux", "screen", "none"}. `auto` prefers tmux,
    falls back to screen, then `none`.
    """
    preference = (preference or "auto").lower()
    if preference == "tmux":
        if not shutil.which("tmux"):
            raise MultiplexerError("session_multiplexer=tmux but tmux not on PATH")
        return TmuxBackend()
    if preference == "screen":
        if not shutil.which("screen"):
            raise MultiplexerError("session_multiplexer=screen but screen not on PATH")
        return ScreenBackend()
    if preference == "none":
        return NoopBackend()
    # auto
    if shutil.which("tmux"):
        return TmuxBackend()
    if shutil.which("screen"):
        return ScreenBackend()
    return NoopBackend()
