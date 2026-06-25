"""Conda / Docker / Singularity activation prefix builder — Phase 3 #7.

Resolution order (first match wins):
  1. caller override (`env=` or `container=`)
  2. project default in `.baird/project.yaml` → `env:` block (one of conda/docker/singularity)
  3. auto-detect from project root: `environment.yml` (→ conda), `Dockerfile`, `*.sif`
  4. bare execution — only if `env.bare: true` opted in, else a loud warning

Each resolved `EnvSpec` knows how to render a shell prefix that the executor
prepends to every command. Mamba is preferred over conda when both are on PATH
(detected once and cached).
"""

from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


EnvKind = Literal["conda", "docker", "singularity", "bare"]


@dataclass
class EnvSpec:
    kind: EnvKind
    name: str | None = None        # conda env name
    image: str | None = None       # docker image
    sif: str | None = None         # singularity image path
    bind_paths: list[str] = field(default_factory=list)
    bare_warning: bool = False     # was bare chosen because we couldn't find anything?

    def render_prefix(self, *, cwd: str | None = None) -> str:
        """Shell-quoted activation prefix to prepend to a command."""
        if self.kind == "conda":
            assert self.name
            tool = "mamba" if shutil.which("mamba") else "conda"
            return f'eval "$({tool} shell.bash hook)" && {tool} activate {shlex.quote(self.name)} && '
        if self.kind == "docker":
            assert self.image
            mount = ""
            if cwd:
                mount = f"-v {shlex.quote(cwd)}:/work -w /work "
            return f"docker run --rm {mount}{shlex.quote(self.image)} bash -lc "
        if self.kind == "singularity":
            assert self.sif
            binds = " ".join(f"-B {shlex.quote(p)}" for p in self.bind_paths)
            return f"singularity exec {binds} {shlex.quote(self.sif)} ".rstrip() + " bash -lc "
        return ""

    def version_descriptor(self) -> str:
        """Short tag for the action row's `env_hash` companion field — not the
        full hash itself; the full hash is computed elsewhere."""
        if self.kind == "conda":
            return f"conda:{self.name}"
        if self.kind == "docker":
            return f"docker:{self.image}"
        if self.kind == "singularity":
            return f"singularity:{self.sif}"
        return "bare"


# ---- Resolution --------------------------------------------------------


def resolve_env(
    *,
    project_root: Path | None,
    project_env_cfg: dict | None = None,
    override: EnvSpec | None = None,
) -> EnvSpec:
    """Return the EnvSpec for the active project.

    `project_env_cfg` is the `env:` block from `.baird/project.yaml`, if any.
    """
    if override is not None:
        return override

    if project_env_cfg:
        return _from_project_cfg(project_env_cfg)

    if project_root is not None:
        detected = _auto_detect(project_root)
        if detected is not None:
            return detected

    return EnvSpec(kind="bare", bare_warning=True)


def _from_project_cfg(cfg: dict) -> EnvSpec:
    if "conda" in cfg:
        return EnvSpec(kind="conda", name=cfg["conda"])
    if "docker" in cfg:
        return EnvSpec(kind="docker", image=cfg["docker"])
    if "singularity" in cfg:
        binds = cfg.get("bind_paths") or []
        return EnvSpec(kind="singularity", sif=cfg["singularity"], bind_paths=list(binds))
    if cfg.get("bare") is True:
        return EnvSpec(kind="bare")
    return EnvSpec(kind="bare", bare_warning=True)


def _auto_detect(root: Path) -> EnvSpec | None:
    env_yml = root / "environment.yml"
    if not env_yml.exists():
        env_yml = root / "environment.yaml"
    if env_yml.exists():
        name = _parse_env_yml_name(env_yml)
        if name:
            return EnvSpec(kind="conda", name=name)

    if (root / "Dockerfile").exists():
        # Image name unknown until built — leave None; caller may supply via override.
        return EnvSpec(kind="docker", image=root.name.lower())

    sifs = list(root.glob("*.sif"))
    if sifs:
        return EnvSpec(kind="singularity", sif=str(sifs[0]))

    return None


def _parse_env_yml_name(path: Path) -> str | None:
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("name:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None
