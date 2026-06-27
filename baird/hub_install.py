"""Install / uninstall systemd units for the hub + local watchdog daemon.

`baird hub install` writes the unit files and enables them so the hub and
daemon survive reboots and restart on failure. Two scopes:

- ``--user``  (default): writes to ``~/.config/systemd/user/`` and uses
  ``systemctl --user``. No sudo. Services stop at logout unless ``loginctl
  enable-linger`` is set (the CLI prints the one-liner).
- ``--system``: writes to ``/etc/systemd/system/`` and uses ``systemctl``.
  Requires sudo. Right for a dedicated always-on machine.

Mirrors the dataclass + injected ``CommandRunner`` pattern from
``baird.satellite`` so the same tests can stub subprocess calls.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .satellite import CommandRunner, _default_runner

Scope = Literal["user", "system"]


HUB_UNIT_TEMPLATE = """[Unit]
Description=BAIRD hub (FastAPI registry + memory + proxy + scheduler)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{user_line}WorkingDirectory={home}
Environment=BAIRD_HOME={baird_home}
ExecStart={baird_bin} hub serve
Restart=on-failure
RestartSec=3
StandardOutput=append:{baird_home}/hub.log
StandardError=append:{baird_home}/hub.log

[Install]
WantedBy={wanted_by}
"""

DAEMON_UNIT_TEMPLATE = """[Unit]
Description=BAIRD local watchdog + executor daemon
After=network-online.target baird-hub.service
Wants=network-online.target
Requires=baird-hub.service

[Service]
Type=simple
{user_line}WorkingDirectory={home}
Environment=BAIRD_HOME={baird_home}
ExecStart={baird_bin} daemon
Restart=on-failure
RestartSec=3
StandardOutput=append:{baird_home}/daemon.log
StandardError=append:{baird_home}/daemon.log

[Install]
WantedBy={wanted_by}
"""


@dataclass
class InstallSpec:
    scope: Scope = "user"
    baird_bin: str = field(default_factory=lambda: _default_baird_bin())
    baird_home: Path = field(default_factory=lambda: _default_baird_home())
    home: Path = field(default_factory=Path.home)
    user: str = field(default_factory=lambda: os.environ.get("USER", ""))
    # Override for tests so we don't touch the real filesystem.
    system_unit_dir: Path = Path("/etc/systemd/system")
    user_unit_dir: Path = field(
        default_factory=lambda: Path.home() / ".config/systemd/user"
    )


def _default_baird_bin() -> str:
    """Use the `baird` CLI that lives next to the current Python interpreter.

    Falls back to bare ``baird`` if the sibling script isn't present (unusual
    — only happens if invoked via ``python -m baird.cli`` from a non-venv
    interpreter that hasn't installed the entry point).
    """
    candidate = Path(sys.executable).with_name("baird")
    if candidate.exists():
        return str(candidate)
    return "baird"


def _default_baird_home() -> Path:
    env = os.environ.get("BAIRD_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".baird"


def _render(template: str, spec: InstallSpec) -> str:
    if spec.scope == "system":
        user_line = f"User={spec.user}\nGroup={spec.user}\n" if spec.user else ""
        wanted_by = "multi-user.target"
    else:
        user_line = ""
        wanted_by = "default.target"
    return template.format(
        user_line=user_line,
        wanted_by=wanted_by,
        home=spec.home,
        baird_home=spec.baird_home,
        baird_bin=spec.baird_bin,
    )


def render_units(spec: InstallSpec) -> dict[str, str]:
    """Return ``{unit_filename: contents}`` for the units the install would write."""
    return {
        "baird-hub.service": _render(HUB_UNIT_TEMPLATE, spec),
        "baird-daemon.service": _render(DAEMON_UNIT_TEMPLATE, spec),
    }


def _unit_dir(spec: InstallSpec) -> Path:
    return spec.system_unit_dir if spec.scope == "system" else spec.user_unit_dir


def _systemctl(spec: InstallSpec, *args: str) -> list[str]:
    if spec.scope == "system":
        return ["sudo", "systemctl", *args]
    return ["systemctl", "--user", *args]


def install(
    spec: InstallSpec | None = None, *, run: CommandRunner = _default_runner
) -> list[str]:
    """Write unit files and ``enable --now`` them. Returns the unit names."""
    spec = spec or InstallSpec()
    unit_dir = _unit_dir(spec)
    units = render_units(spec)

    if spec.scope == "system":
        # We can't write to /etc/systemd/system without root, so shell out via
        # `sudo tee` for each unit. One sudo prompt per file is acceptable —
        # the credential is cached after the first.
        for name, body in units.items():
            target = unit_dir / name
            r = run(["sudo", "tee", str(target)], input=body)
            if r.returncode != 0:
                raise RuntimeError(
                    f"failed to write {target}: {r.stderr.strip() or r.stdout.strip()}"
                )
    else:
        unit_dir.mkdir(parents=True, exist_ok=True)
        for name, body in units.items():
            (unit_dir / name).write_text(body)

    _check(run(_systemctl(spec, "daemon-reload")))
    _check(
        run(_systemctl(spec, "enable", "--now", "baird-hub.service", "baird-daemon.service"))
    )
    return list(units)


def uninstall(
    spec: InstallSpec | None = None, *, run: CommandRunner = _default_runner
) -> None:
    spec = spec or InstallSpec()
    unit_dir = _unit_dir(spec)
    # `disable --now` is idempotent; ignore failures so an already-removed
    # install doesn't error out.
    run(_systemctl(spec, "disable", "--now", "baird-hub.service", "baird-daemon.service"))
    for name in ("baird-hub.service", "baird-daemon.service"):
        target = unit_dir / name
        if spec.scope == "system":
            if target.exists():
                run(["sudo", "rm", "-f", str(target)])
        else:
            target.unlink(missing_ok=True)
    run(_systemctl(spec, "daemon-reload"))


def _check(result) -> None:
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout or "systemctl failed").strip()
        )
