"""Resolution of BAIRD's per-install state directory.

The user can run multiple BAIRD installs side-by-side (e.g. a development
checkout + a production checkout) by pointing each at its own state dir
via the `BAIRD_HOME` environment variable. Falls back to `~/.baird`.
"""

from __future__ import annotations

import os
from pathlib import Path


def baird_home() -> Path:
    """Return the state directory (`$BAIRD_HOME` or `~/.baird`)."""
    return Path(os.environ.get("BAIRD_HOME", "~/.baird")).expanduser()


def host_yaml_path() -> Path:
    return baird_home() / "host.yaml"


def hub_config_path() -> Path:
    return baird_home() / "config.yaml"


def tasks_dir() -> Path:
    return baird_home() / "tasks"


def registry_db_path() -> Path:
    return baird_home() / "registry.sqlite"


def memory_db_path() -> Path:
    return baird_home() / "memory.sqlite"


def lance_dir_path() -> Path:
    """`<baird_home>/lance/` — LanceDB tables."""
    return baird_home() / "lance"


def secrets_env_path() -> Path:
    """`<baird_home>/secrets.env` — KEY=value, one per line. chmod 600 it.

    Loaded into os.environ on hub startup so the hub picks up its credentials
    regardless of which shell / systemd unit / cron starts it.
    """
    return baird_home() / "secrets.env"


def load_secrets_env(path: Path | None = None) -> dict[str, str]:
    """Parse a simple `KEY=value` file and return the dict. Missing file → {}.

    Lines starting with `#` are comments. Quotes around the value are stripped
    if matched. No shell expansion (no `$VAR`, no backticks) — keep it boring.
    Existing env vars take precedence; we never overwrite what the user set
    explicitly in the calling shell.
    """
    p = path or secrets_env_path()
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        out[k] = v
    return out


def apply_secrets_env(path: Path | None = None) -> list[str]:
    """Apply `load_secrets_env()` to `os.environ`. Returns the keys added."""
    added: list[str] = []
    for k, v in load_secrets_env(path).items():
        if k not in os.environ:
            os.environ[k] = v
            added.append(k)
    return added
