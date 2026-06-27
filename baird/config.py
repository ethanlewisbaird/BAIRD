"""Config loading.

BAIRD reads layered YAML config:

- Built-in defaults shipped with the package.
- User-wide overrides at `<baird_home>/config.yaml`.
- Per-host config at `<baird_home>/host.yaml` (volume map, session multiplexer, scope filter).
- Per-project overrides at `<project_root>/.baird/project.yaml`.

`baird_home` is `$BAIRD_HOME` if set, else `~/.baird`. Most-specific layer wins
on a per-key basis.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from . import paths


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
    # Inbound bearer the EXECUTOR on this host requires. Set on satellites the
    # hub will drive; null means the executor denies all calls.
    auth_token: str | None = None
    # Outbound bearer this host sends to the hub. If null, falls back to
    # `auth_token` (compat). Set both on satellites that talk to a hub that
    # requires auth.
    hub_auth_token: str | None = None
    # Route OpenRouter calls through the hub's proxy instead of calling
    # OpenRouter directly. Lets a satellite work without holding the key.
    use_hub_for_models: bool = False
    # If set, the daemon spawns the executor service on this address. Example:
    # "0.0.0.0:8765" (Tailscale-only in practice — bind to the tailnet iface).
    executor_listen: str | None = None

    def effective_hub_token(self) -> str | None:
        return self.hub_auth_token or self.auth_token


class HubConfig(BaseModel):
    """Hub-side config loaded from `<baird_home>/config.yaml`."""

    listen: str = "127.0.0.1:8000"
    registry_db: str = Field(default_factory=lambda: str(paths.registry_db_path()))
    memory_db: str = Field(default_factory=lambda: str(paths.memory_db_path()))
    daily_total_usd: float = 5.0
    daily_per_task_default_usd: float = 0.5
    # When set, every hub route except /health requires `Authorization: Bearer
    # <auth_token>`. When null the hub is open (current behaviour).
    auth_token: str | None = None
    # Where the model proxy forwards to. Override for staging or a corporate
    # gateway. The hub's OPENROUTER_API_KEY env var supplies the upstream key.
    openrouter_url: str = "https://openrouter.ai/api/v1"
    # Semantic /recall (optional). When `recall_enabled`, the hub maintains a
    # LanceDB vector index in <baird_home>/lance/ and answers /recall with a
    # hybrid SQL + vector merge. Requires `pip install baird[recall]`.
    recall_enabled: bool = True
    embedder_model: str = "BAAI/bge-small-en-v1.5"
    embedder_device: str = "cpu"


# ----- Loading helpers -----


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data


def load_host_config(path: Path | None = None) -> HostConfig:
    path = path or paths.host_yaml_path()
    return HostConfig(**_load_yaml(path))


def load_hub_config(path: Path | None = None) -> HubConfig:
    path = path or paths.hub_config_path()
    return HubConfig(**_load_yaml(path))
