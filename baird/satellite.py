"""Satellite enrolment + tunnel management.

`baird satellite enroll <ssh_host>` — run from the hub, drives the satellite-side
install of BAIRD, writes `~/.baird/host.yaml` with the hub's auth token already
filled in, and stands up the persistent SSH tunnel locally so the satellite
can reach the hub without any further config.

`baird satellite list / remove` — manage the systemd-user units we install.

Design intent: a single command from the hub turns a fresh machine into a
working satellite. Internals are split into small typed dataclasses + pure
functions so tests can drive everything with a stubbed `run_ssh`.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import paths
from .config import load_hub_config


# ---------- subprocess seam (overridable for tests) ----------------------


class CommandRunner:
    """Callable for subprocess invocations. Lets tests stub network/system calls."""

    def __call__(
        self, cmd: list[str], *, input: Optional[str] = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, input=input, capture_output=True, text=True, check=False
        )


_default_runner = CommandRunner()


# ---------- satellite registry (which hosts are enrolled) ----------------


def _registry_path() -> Path:
    return paths.baird_home() / "satellites.json"


def load_registry() -> dict[str, dict]:
    p = _registry_path()
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def save_registry(reg: dict[str, dict]) -> None:
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2, sort_keys=True))


def next_available_port(reg: dict[str, dict], start: int = 8766) -> int:
    """Pick a hub-side forward port not already assigned and not in use."""
    used = {entry.get("local_fwd_port") for entry in reg.values()}
    port = start
    while port in used or _port_in_use(port):
        port += 1
    return port


def _port_in_use(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


# ---------- tunnel install (hub-side systemd-user units) -----------------


TUNNEL_UNIT = """[Unit]
Description=BAIRD SSH tunnel to %i
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=%h/.config/baird/tunnel-%i.env
ExecStart=/usr/bin/ssh -N \\
    -o ServerAliveInterval=30 \\
    -o ServerAliveCountMax=3 \\
    -o ExitOnForwardFailure=yes \\
    -o StreamLocalBindUnlink=yes \\
    -o BatchMode=yes \\
    -R 8000:localhost:8000 \\
    -L ${LOCAL_FWD_PORT}:localhost:8765 \\
    %i
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


@dataclass
class TunnelSpec:
    ssh_host: str
    local_fwd_port: int
    systemd_user_dir: Path = field(default_factory=lambda: Path.home() / ".config/systemd/user")
    baird_config_dir: Path = field(default_factory=lambda: Path.home() / ".config/baird")


def install_tunnel(spec: TunnelSpec, *, run: CommandRunner = _default_runner) -> None:
    spec.systemd_user_dir.mkdir(parents=True, exist_ok=True)
    spec.baird_config_dir.mkdir(parents=True, exist_ok=True)

    unit_file = spec.systemd_user_dir / "baird-tunnel@.service"
    if not unit_file.exists():
        unit_file.write_text(TUNNEL_UNIT)

    env_file = spec.baird_config_dir / f"tunnel-{spec.ssh_host}.env"
    env_file.write_text(f"LOCAL_FWD_PORT={spec.local_fwd_port}\n")

    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", f"baird-tunnel@{spec.ssh_host}"])


def remove_tunnel(spec: TunnelSpec, *, run: CommandRunner = _default_runner) -> None:
    run(["systemctl", "--user", "disable", "--now", f"baird-tunnel@{spec.ssh_host}"])
    env_file = spec.baird_config_dir / f"tunnel-{spec.ssh_host}.env"
    env_file.unlink(missing_ok=True)


def tunnel_status(ssh_host: str, *, run: CommandRunner = _default_runner) -> str:
    """One of: active | inactive | failed | unknown."""
    r = run(["systemctl", "--user", "is-active", f"baird-tunnel@{ssh_host}"])
    return (r.stdout or r.stderr or "unknown").strip()


# ---------- satellite install (drives the remote machine via SSH) --------


@dataclass
class EnrollSpec:
    ssh_host: str
    host_id: str
    git_url: str = "https://github.com/ethanlewisbaird/BAIRD.git"
    git_ref: str = "main"
    remote_baird_dir: str = "~/code/BAIRD"
    remote_baird_home: str = "~/.baird"
    remote_watch_root: str = "~/projects"
    use_hub_for_models: bool = True
    executor_listen: str = "127.0.0.1:8765"
    hub_url_from_satellite: str = "http://127.0.0.1:8000"
    hub_auth_token: Optional[str] = None
    local_fwd_port: Optional[int] = None  # auto-assigned if None


HOST_YAML_TEMPLATE = """host_id: {host_id}
hub_url: {hub_url}
session_multiplexer: auto

# Inbound bearer the EXECUTOR on this host requires. null = deny inbound calls.
auth_token: null

# Outbound bearer this satellite sends to the hub.
hub_auth_token: {hub_auth_token}

# Route OpenRouter calls through the hub proxy instead of holding the key here.
use_hub_for_models: {use_hub_for_models}

executor_listen: {executor_listen}

volumes:
  - id: {host_id}:/home
    mount: {remote_home}
    shared: false

watch:
  roots:
    - {watch_root}
  deny:
    - "**/.git/**"
    - "**/__pycache__/**"
    - "**/.snakemake/**"
    - "**/.nextflow/**"
    - "**/.ipynb_checkpoints/**"
"""


def _yaml_str(value: Optional[str]) -> str:
    """Format an optional string for YAML — quoted if present, `null` otherwise."""
    if value is None:
        return "null"
    return json.dumps(value)


def _yaml_bool(value: bool) -> str:
    return "true" if value else "false"


def _render_host_yaml(
    spec: EnrollSpec, *, remote_home: str
) -> str:
    return HOST_YAML_TEMPLATE.format(
        host_id=spec.host_id,
        hub_url=spec.hub_url_from_satellite,
        hub_auth_token=_yaml_str(spec.hub_auth_token),
        use_hub_for_models=_yaml_bool(spec.use_hub_for_models),
        executor_listen=_yaml_str(spec.executor_listen),
        remote_home=remote_home,
        watch_root=spec.remote_watch_root,
    )


# Bash sent to the satellite. Designed to be safe to re-run.
SATELLITE_BOOTSTRAP = r"""set -e
BAIRD_DIR="{remote_baird_dir}"
BAIRD_HOME="{remote_baird_home}"
GIT_URL="{git_url}"
GIT_REF="{git_ref}"

mkdir -p "$BAIRD_HOME" "$(dirname "$BAIRD_DIR")"

if [ ! -d "$BAIRD_DIR/.git" ]; then
    git clone "$GIT_URL" "$BAIRD_DIR" 2>&1 | tail -3
fi

cd "$BAIRD_DIR"
git fetch --tags --quiet origin
git checkout --quiet "$GIT_REF" 2>&1 | tail -2
# Refresh to the tip of branches; tags are immutable.
if git show-ref --verify --quiet "refs/remotes/origin/$GIT_REF"; then
    git reset --hard --quiet "origin/$GIT_REF"
fi

# Prefer uv when available — it brings its own Python.
if [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
elif command -v uv >/dev/null 2>&1; then
    UV="uv"
else
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
    UV="$HOME/.local/bin/uv"
fi
"$UV" venv --python 3.11 --quiet 2>&1 | tail -2
"$UV" pip install -e . --quiet 2>&1 | tail -3

# Idempotent alias in .bashrc.
if ! grep -q "BAIRD_HOME=" "$HOME/.bashrc" 2>/dev/null; then
    {{
        echo
        echo "# BAIRD"
        echo "alias baird='BAIRD_HOME=$BAIRD_HOME $BAIRD_DIR/.venv/bin/baird'"
    }} >> "$HOME/.bashrc"
fi

echo "OK $HOME"
"""


@dataclass
class EnrollResult:
    host_id: str
    ssh_host: str
    remote_home: str
    local_fwd_port: int
    health_ok: bool
    detail: str = ""


def enroll(
    spec: EnrollSpec,
    *,
    run: CommandRunner = _default_runner,
    tunnel_spec_cls: type[TunnelSpec] = TunnelSpec,
) -> EnrollResult:
    """Drive a remote install + tunnel + verification.

    Steps:
      1. Stand up the hub-side SSH tunnel (idempotent; assigns a forward port).
      2. SSH out and run the bootstrap script — installs BAIRD via uv.
      3. Render host.yaml with the hub's auth token already filled in and
         scp/write it into the satellite's $BAIRD_HOME.
      4. Probe round-trip by running `baird --version` over the tunnel.
    """
    reg = load_registry()
    port = spec.local_fwd_port or next_available_port(reg)

    tspec = tunnel_spec_cls(ssh_host=spec.ssh_host, local_fwd_port=port)
    install_tunnel(tspec, run=run)

    # Step 2: bootstrap the satellite.
    script = SATELLITE_BOOTSTRAP.format(
        remote_baird_dir=spec.remote_baird_dir,
        remote_baird_home=spec.remote_baird_home,
        git_url=spec.git_url,
        git_ref=spec.git_ref,
    )
    r = run(
        ["ssh", "-o", "BatchMode=yes", spec.ssh_host, "bash", "-s"],
        input=script,
    )
    if r.returncode != 0:
        return EnrollResult(
            host_id=spec.host_id,
            ssh_host=spec.ssh_host,
            remote_home="",
            local_fwd_port=port,
            health_ok=False,
            detail=f"bootstrap failed: {r.stderr or r.stdout}",
        )

    remote_home = ""
    for line in (r.stdout or "").splitlines():
        if line.startswith("OK "):
            remote_home = line[3:].strip()
            break
    if not remote_home:
        remote_home = "/home/" + spec.ssh_host  # rough fallback

    # Step 3: write host.yaml on the satellite.
    yaml_body = _render_host_yaml(spec, remote_home=remote_home)
    write_cmd = f"mkdir -p {spec.remote_baird_home} && cat > {spec.remote_baird_home}/host.yaml"
    r2 = run(
        ["ssh", "-o", "BatchMode=yes", spec.ssh_host, write_cmd],
        input=yaml_body,
    )
    if r2.returncode != 0:
        return EnrollResult(
            host_id=spec.host_id,
            ssh_host=spec.ssh_host,
            remote_home=remote_home,
            local_fwd_port=port,
            health_ok=False,
            detail=f"host.yaml write failed: {r2.stderr}",
        )

    # Step 4: prove round-trip.
    probe = run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            spec.ssh_host,
            f"BAIRD_HOME={spec.remote_baird_home} "
            f"{spec.remote_baird_dir}/.venv/bin/baird project list",
        ]
    )
    health_ok = probe.returncode == 0 and "Traceback" not in (probe.stdout + probe.stderr)

    reg[spec.host_id] = {
        "ssh_host": spec.ssh_host,
        "remote_home": remote_home,
        "remote_baird_dir": spec.remote_baird_dir,
        "local_fwd_port": port,
        "use_hub_for_models": spec.use_hub_for_models,
        "git_ref": spec.git_ref,
    }
    save_registry(reg)

    return EnrollResult(
        host_id=spec.host_id,
        ssh_host=spec.ssh_host,
        remote_home=remote_home,
        local_fwd_port=port,
        health_ok=health_ok,
        detail=(probe.stdout + probe.stderr).strip()[:500],
    )


def enroll_spec_from_local(
    ssh_host: str,
    *,
    host_id: Optional[str] = None,
    git_ref: str = "main",
) -> EnrollSpec:
    """Build an EnrollSpec from the running hub's config — pulls hub_auth_token
    out of the local config.yaml so the user never has to type it."""
    hub_cfg = load_hub_config()
    return EnrollSpec(
        ssh_host=ssh_host,
        host_id=host_id or ssh_host,
        hub_auth_token=hub_cfg.auth_token,
        git_ref=git_ref,
    )
