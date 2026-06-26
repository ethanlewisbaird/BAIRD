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
