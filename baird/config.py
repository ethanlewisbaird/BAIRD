"""Config loading.

BAIRD reads layered YAML config:

- Built-in defaults shipped with the package.
- User-wide overrides at `~/.baird/config.yaml`.
- Per-host config at `~/.baird/host.yaml` (volume map, session multiplexer, scope filter).
- Per-project overrides at `<project_root>/.baird/project.yaml`.

Most-specific layer wins on a per-key basis.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


# ----- Schemas (kept minimal in v0; fields will grow as phases land) -----


class VolumeSpec(BaseModel):
    """A storage volume on a host — e.g. `cluster:/work`."""

    id: str
    mount: str
    shared: bool = False


class WatchSpec(BaseModel):
    roots: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class HostConfig(BaseModel):
    """Per-host daemon config loaded from `~/.baird/host.yaml`."""

    host_id: str
    volumes: list[VolumeSpec] = Field(default_factory=list)
    watch: WatchSpec = Field(default_factory=WatchSpec)
    session_multiplexer: str = "auto"  # auto | tmux | screen | none
    hub_url: str = "http://localhost:8000"
    auth_token: str | None = None
    # If set, the daemon spawns the executor service on this address. Example:
    # "0.0.0.0:8765" (Tailscale-only in practice — bind to the tailnet iface).
    executor_listen: str | None = None


class HubConfig(BaseModel):
    """Hub-side config loaded from `~/.baird/config.yaml`."""

    listen: str = "127.0.0.1:8000"
    registry_db: str = "~/.baird/registry.sqlite"
    memory_db: str = "~/.baird/memory.sqlite"
    daily_total_usd: float = 5.0
    daily_per_task_default_usd: float = 0.5


# ----- Loading helpers -----


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data


def load_host_config(path: Path | None = None) -> HostConfig:
    path = path or Path("~/.baird/host.yaml").expanduser()
    return HostConfig(**_load_yaml(path))


def load_hub_config(path: Path | None = None) -> HubConfig:
    path = path or Path("~/.baird/config.yaml").expanduser()
    return HubConfig(**_load_yaml(path))
