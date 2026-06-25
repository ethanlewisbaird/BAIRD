"""Safe/destructive classifier — Phase 3 design, sub-decision #6.

Three tiers:

  Tier 1 (`safe`)        — read-only ops: read_file, run_command for known-safe
                            tools (ls, cat, git status, head, …). Auto-run in
                            interactive mode.
  Tier 2 (`project`)     — writes scoped *inside* the active project root, or
                            running scoped tools (pytest, make, etc.). Auto in
                            interactive mode with a warning; prompt in
                            background-agent mode (Phase 4).
  Tier 3 (`destructive`) — everything else: writes outside the project root,
                            destructive commands (`rm -rf`, `git push --force`,
                            `dd`, package installs, `sudo` …). Always prompt.

The classifier ships a default policy and consults per-project overrides from
`.baird/project.yaml` → `permissions:`. Most-specific wins. Policy is reread on
every call so the gate can be edited without restarting the executor.

NB: `pip install` / `conda install` / `apt install` always classify as
destructive regardless of overrides — the right path is to add the dep to the
env spec and rebuild the env via a normal diff → approval cycle.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Tier(str, Enum):
    SAFE = "safe"
    PROJECT = "project"
    DESTRUCTIVE = "destructive"


@dataclass
class Decision:
    tier: Tier
    reason: str
    rule: str | None = None  # which rule matched


@dataclass
class PolicyOverride:
    """A `permissions:` block from a project.yaml — raises or lowers tiers for
    matching commands. `command_regex` is a Python regex against the full
    command string."""

    command_regex: str
    tier: Tier
    reason: str = "project override"


@dataclass
class Policy:
    safe_commands: list[str] = field(default_factory=list)  # regexes
    project_commands: list[str] = field(default_factory=list)  # regexes
    destructive_commands: list[str] = field(default_factory=list)  # regexes
    always_destructive: list[str] = field(default_factory=list)  # regexes, can't be overridden
    overrides: list[PolicyOverride] = field(default_factory=list)


# --- Default policy ------------------------------------------------------


def default_policy() -> Policy:
    return Policy(
        safe_commands=[
            r"^\s*ls(\s|$)",
            r"^\s*pwd(\s|$)",
            r"^\s*cat\s",
            r"^\s*head\s",
            r"^\s*tail\s",
            r"^\s*wc\s",
            r"^\s*grep\s",
            r"^\s*rg\s",
            r"^\s*find\s",
            r"^\s*file\s",
            r"^\s*echo\s",
            r"^\s*git\s+(status|log|diff|show|branch|remote|config\s+--get)",
            r"^\s*tree(\s|$)",
            r"^\s*which\s",
            r"^\s*type\s",
            r"^\s*conda\s+(env\s+list|list|info)",
            r"^\s*mamba\s+(env\s+list|list|info)",
            r"^\s*samtools\s+view\s+-H",
            r"^\s*samtools\s+(idxstats|flagstat|stats)",
            r"^\s*bcftools\s+(stats|view\s+-h)",
        ],
        project_commands=[
            r"^\s*pytest(\s|$)",
            r"^\s*python\s+-m\s+pytest(\s|$)",
            r"^\s*ruff\s+(check|format)(\s|$)",
            r"^\s*black\s",
            r"^\s*mypy\s",
            r"^\s*Rscript\s",
            r"^\s*Rscript\s+-e\s",
            r"^\s*make(\s|$)",
            r"^\s*snakemake(\s|$)",
            r"^\s*nextflow\s+run(\s|$)",
            r"^\s*git\s+(add|commit|checkout|switch|stash|restore)\b",
            r"^\s*python\s+\S+\.py\b",
        ],
        destructive_commands=[
            r"^\s*rm\s",
            r"^\s*mv\s",
            r"^\s*cp\s",
            r"^\s*chmod\s",
            r"^\s*chown\s",
            r"^\s*git\s+reset\s+--hard\b",
            r"^\s*git\s+clean\b",
            r"^\s*git\s+push\b",
            r"^\s*dd\s",
            r"^\s*mkfs",
            r"^\s*kill(all)?\s",
        ],
        always_destructive=[
            r"\bsudo\b",
            r"^\s*pip\s+install\b",
            r"^\s*pip3\s+install\b",
            r"^\s*conda\s+install\b",
            r"^\s*mamba\s+install\b",
            r"^\s*apt(-get)?\s+(install|remove|purge|upgrade|update)\b",
            r"^\s*brew\s+(install|upgrade|uninstall)\b",
            r"^\s*npm\s+install\b",
            r"^\s*git\s+push\s+(-f|--force|--force-with-lease)\b",
            r"^\s*rm\s+-rf?\s+/",
        ],
    )


# --- Classification ------------------------------------------------------


def _match_any(patterns: list[str], cmd: str) -> str | None:
    for p in patterns:
        if re.search(p, cmd):
            return p
    return None


def classify_command(
    command: str,
    *,
    policy: Policy | None = None,
    project_overrides: list[PolicyOverride] | None = None,
) -> Decision:
    """Classify a shell command string. Resolution order (first match wins):

    1. `always_destructive` — non-overridable.
    2. Project overrides (most specific layer).
    3. Default policy: safe → project → destructive.
    4. Fallback: destructive (deny-by-default).
    """
    policy = policy or default_policy()
    cmd = command.strip()

    m = _match_any(policy.always_destructive, cmd)
    if m:
        return Decision(Tier.DESTRUCTIVE, "always-destructive command", rule=m)

    for ov in project_overrides or []:
        if re.search(ov.command_regex, cmd):
            return Decision(ov.tier, ov.reason, rule=ov.command_regex)

    m = _match_any(policy.safe_commands, cmd)
    if m:
        return Decision(Tier.SAFE, "matched safe pattern", rule=m)

    m = _match_any(policy.project_commands, cmd)
    if m:
        return Decision(Tier.PROJECT, "matched project pattern", rule=m)

    m = _match_any(policy.destructive_commands, cmd)
    if m:
        return Decision(Tier.DESTRUCTIVE, "matched destructive pattern", rule=m)

    return Decision(Tier.DESTRUCTIVE, "no safe/project rule matched — denied by default")


# --- Path scoping --------------------------------------------------------


def classify_write(
    target: Path,
    *,
    project_root: Path | None,
) -> Decision:
    """Classify a write to `target`. Writes inside the project root are tier 2
    (project); writes anywhere else are tier 3 (destructive)."""
    target_abs = target.expanduser().resolve()
    if project_root is None:
        return Decision(Tier.DESTRUCTIVE, "no active project — writes ungated", rule="no-project")
    root_abs = project_root.expanduser().resolve()
    try:
        target_abs.relative_to(root_abs)
    except ValueError:
        return Decision(Tier.DESTRUCTIVE, f"write target outside project root ({root_abs})")
    return Decision(Tier.PROJECT, "write inside project root")


# --- Loading project overrides ------------------------------------------


def overrides_from_project_yaml(project_yaml_dict: dict[str, Any]) -> list[PolicyOverride]:
    """Extract `permissions:` from a project.yaml dict, if present.

    Expected shape:

        permissions:
          - command_regex: "^./run_pipeline.sh"
            tier: project
            reason: "vetted pipeline runner"
    """
    raw = (project_yaml_dict.get("permissions") or [])
    out: list[PolicyOverride] = []
    for entry in raw:
        try:
            out.append(
                PolicyOverride(
                    command_regex=entry["command_regex"],
                    tier=Tier(entry["tier"]),
                    reason=entry.get("reason", "project override"),
                )
            )
        except (KeyError, ValueError):
            # Skip malformed entries silently — should be caught by yaml schema validation upstream.
            continue
    return out


# --- Convenience --------------------------------------------------------


def shlex_safe_split(command: str) -> list[str]:
    """Best-effort split — used to peek at the head of a command for debug
    rendering. Falls back to a single-element list on parse failure."""
    try:
        return shlex.split(command)
    except ValueError:
        return [command]
