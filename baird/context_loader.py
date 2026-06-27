"""Repo context loader — Phase 3 design, sub-decision #2.

Builds the per-turn context block for `baird code`:

- Project header (id, name, github, branch, host, cwd)
- Project memory (context paragraph + last N decisions + applicable rules + goals)
- `tree -L 3` of the project root, with subtrees >50 entries auto-collapsed
- Last 10 git commits (oneline)
- `git status --porcelain`
- Recent tier-2 action summaries for this project (pulled from the hub)
- A list of "relevant files" — files the caller explicitly named, plus the
  always-include set (`.baird/project.yaml`, `environment.yml`, `README.md`,
  `CLAUDE.md` if present)

Token budget is approximate — we count words ~ 1.3 × tokens — and prefer to
drop deepest tree subdirs first, then commits, then summaries.

Symbol-map (ctags) is deferred; LSP integration deferred.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory_client import HubClient
from .project_yaml import Location, ProjectYaml, effective_locations, load_project_yaml


ALWAYS_INCLUDE = [
    ".baird/project.yaml",
    "environment.yml",
    "environment.yaml",
    "README.md",
    "CLAUDE.md",
    "pyproject.toml",
]

TREE_COLLAPSE_THRESHOLD = 50
DEFAULT_TOKEN_BUDGET = 6000


@dataclass
class RepoContext:
    project: ProjectYaml
    project_root: Path | None
    branch: str | None
    git_log_lines: list[str] = field(default_factory=list)
    git_status: str = ""
    tree: str = ""
    relevant_files: dict[str, str] = field(default_factory=dict)
    decisions: list[dict] = field(default_factory=list)
    action_summaries: list[dict] = field(default_factory=list)
    rules_summary: list[str] = field(default_factory=list)
    host_id: str | None = None
    locations: list[Location] = field(default_factory=list)


# ---- Gather helpers ----------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True
    )
    return proc.stdout if proc.returncode == 0 else ""


def _git_branch(repo: Path) -> str | None:
    out = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return out or None


def _git_log_oneline(repo: Path, n: int = 10) -> list[str]:
    out = _git(repo, "log", f"-{n}", "--pretty=format:%h %ad %s", "--date=short")
    return [line for line in out.splitlines() if line]


def _git_status(repo: Path) -> str:
    return _git(repo, "status", "--porcelain")


def _build_tree(root: Path, max_depth: int = 3) -> str:
    """Lightweight `tree -L 3` replacement. Collapses any directory with
    >TREE_COLLAPSE_THRESHOLD entries to a single `<…N entries…>` placeholder.

    Skips hidden directories (`.git`, `.baird`, `.snakemake`, …) and common
    build/cache dirs."""
    skip = {".git", ".baird", ".snakemake", ".nextflow", "__pycache__",
            ".ipynb_checkpoints", "node_modules", ".venv", "venv", ".mypy_cache"}
    lines: list[str] = [f"{root.name}/"]

    def walk(d: Path, depth: int, prefix: str) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(
                e for e in d.iterdir() if e.name not in skip and not e.name.startswith(".")
            )
        except (PermissionError, FileNotFoundError):
            return
        if len(entries) > TREE_COLLAPSE_THRESHOLD:
            lines.append(f"{prefix}└── <…{len(entries)} entries collapsed…>")
            return
        for i, entry in enumerate(entries):
            last = i == len(entries) - 1
            connector = "└── " if last else "├── "
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{prefix}{connector}{entry.name}{suffix}")
            if entry.is_dir():
                walk(entry, depth + 1, prefix + ("    " if last else "│   "))

    walk(root, 1, "")
    return "\n".join(lines)


def _read_relevant(root: Path, paths: list[str], max_per_file_chars: int = 4000) -> dict[str, str]:
    out: dict[str, str] = {}
    seen: set[str] = set()
    for rel in paths:
        if rel in seen:
            continue
        seen.add(rel)
        p = (root / rel).resolve()
        # Path-scope check: refuse to read outside root.
        try:
            p.relative_to(root.resolve())
        except ValueError:
            continue
        if not p.is_file():
            continue
        try:
            txt = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if len(txt) > max_per_file_chars:
            txt = txt[:max_per_file_chars] + f"\n…[truncated, {len(txt)} chars total]"
        out[rel] = txt
    return out


def _load_locations(project: ProjectYaml, hub: HubClient | None) -> list[Location]:
    """Prefer hub-side locations (they're the canonical source post-Slice A);
    fall back to whatever's in the project yaml (which may be the legacy
    `checkout_hosts` field) when the hub is unreachable."""
    if hub is not None:
        try:
            rows = hub.list_project_locations(project.id)
            return [
                Location(host=r["host"], path=r["path"], role=r.get("role"))
                for r in rows
            ]
        except Exception:
            pass
    return effective_locations(project)


def _rules_summary(project: ProjectYaml) -> list[str]:
    return [f"[{r.severity}] {r.id}: {r.description}" for r in project.rules]


# ---- Public entrypoint -------------------------------------------------


def load_repo_context(
    project_root: Path,
    *,
    hub: HubClient | None = None,
    extra_files: list[str] | None = None,
    n_decisions: int = 5,
    n_action_summaries: int = 5,
    host_id: str | None = None,
) -> RepoContext:
    """Gather the per-turn context block for `baird code`.

    `hub` may be None for offline/test use — decisions and action summaries
    will then be empty.
    """
    project_root = project_root.expanduser().resolve()
    yaml_path = project_root / ".baird" / "project.yaml"
    project = load_project_yaml(yaml_path)

    extras = list(extra_files or [])
    relevant = ALWAYS_INCLUDE + extras
    relevant_files = _read_relevant(project_root, relevant)

    decisions: list[dict] = []
    summaries: list[dict] = []
    if hub is not None:
        try:
            decisions = hub.list_decisions(project.id, limit=n_decisions)
        except Exception:
            decisions = []
        try:
            actions = hub.list_actions(project_id=project.id, limit=n_action_summaries * 3)
            summaries = [a for a in actions if a.get("summary")][:n_action_summaries]
        except Exception:
            summaries = []

    return RepoContext(
        project=project,
        project_root=project_root,
        branch=_git_branch(project_root),
        git_log_lines=_git_log_oneline(project_root, n=10),
        git_status=_git_status(project_root),
        tree=_build_tree(project_root),
        relevant_files=relevant_files,
        decisions=decisions,
        action_summaries=summaries,
        rules_summary=_rules_summary(project),
        host_id=host_id or os.uname().nodename,
        locations=_load_locations(project, hub),
    )


def lite_repo_context(
    project: ProjectYaml,
    *,
    hub: HubClient | None = None,
    host_id: str | None = None,
    n_decisions: int = 5,
    n_action_summaries: int = 5,
) -> RepoContext:
    """RepoContext for a project without a local repo checkout.

    Used by `baird code --project <id>`, the scratch project, and the
    `/project` slash-command switch. Pulls decisions and action summaries
    from the hub if available; skips git/tree/file gathering entirely.
    """
    decisions: list[dict] = []
    summaries: list[dict] = []
    if hub is not None:
        try:
            decisions = hub.list_decisions(project.id, limit=n_decisions)
        except Exception:
            decisions = []
        try:
            actions = hub.list_actions(project_id=project.id, limit=n_action_summaries * 3)
            summaries = [a for a in actions if a.get("summary")][:n_action_summaries]
        except Exception:
            summaries = []
    return RepoContext(
        project=project,
        project_root=None,
        branch=None,
        git_log_lines=[],
        git_status="",
        tree="",
        relevant_files={},
        decisions=decisions,
        action_summaries=summaries,
        rules_summary=_rules_summary(project),
        host_id=host_id or os.uname().nodename,
        locations=_load_locations(project, hub),
    )


# ---- Rendering ---------------------------------------------------------


# Placeholder for the header when a project has no locations attached. The
# previous behaviour was to fall through to `ctx.host_id` (the hub's own
# hostname), which made the model believe the project lived on the hub.
NO_LOCATIONS_PLACEHOLDER = "(no locations set — use /project add-location)"


def project_host_for_display(ctx: RepoContext) -> str:
    """The host string to show in the project header.

    When the project has any locations attached, use the first one's host
    (treat it as the "active" or default location for surfacing purposes).
    When there are none, render an explicit placeholder so neither the user
    nor the model mistakes the hub's hostname for the project's location.
    """
    if ctx.locations:
        return ctx.locations[0].host
    return NO_LOCATIONS_PLACEHOLDER


def render_context(ctx: RepoContext, *, token_budget: int = DEFAULT_TOKEN_BUDGET) -> str:
    """Render a RepoContext as a markdown block. Drops sections in priority
    order if the result would exceed `token_budget` (approx: 1 token ≈ 0.75 words).

    The first version's "drop tree subdirs first" simplification is to drop the
    tree entirely if needed — finer-grained collapse is a follow-up.
    """
    word_limit = int(token_budget * 0.75)

    sections: list[tuple[str, str]] = []

    sections.append(
        (
            "header",
            "\n".join([
                f"# Project: {ctx.project.name} ({ctx.project.id})",
                f"Host: {project_host_for_display(ctx)}",
                f"Root: {ctx.project_root or '(no local checkout)'}",
                f"Branch: {ctx.branch or '(detached)'}",
                f"GitHub: {ctx.project.github or '(none)'}",
            ]),
        )
    )

    sections.append(("context", f"## Context\n\n{ctx.project.context or '(no context paragraph)'}"))

    if ctx.locations:
        loc_lines = [
            f"- `{loc.host}:{loc.path}`" + (f" — {loc.role}" if loc.role else "")
            for loc in ctx.locations
        ]
        sections.append((
            "locations",
            "## Locations\n\nProject spans these (host, path) pairs. Remote tool calls "
            "(read_remote/write_remote/run_on/...) need a `host` argument matching one "
            "of these host_ids:\n\n" + "\n".join(loc_lines),
        ))

    if ctx.project.goals:
        goal_lines = [f"- [{g.status}] {g.text}" for g in ctx.project.goals]
        sections.append(("goals", "## Goals\n\n" + "\n".join(goal_lines)))

    if ctx.decisions:
        d_lines = [
            f"- [{d['created_at'][:10]}] ({d['author']}) {d['text']}"
            for d in ctx.decisions
        ]
        sections.append(("decisions", "## Recent decisions\n\n" + "\n".join(d_lines)))

    if ctx.rules_summary:
        sections.append(("rules", "## Active rules\n\n- " + "\n- ".join(ctx.rules_summary)))

    if ctx.action_summaries:
        a_lines = [
            f"- ({a['tool_name'] or 'cmd'}) {a['summary']}"
            for a in ctx.action_summaries
        ]
        sections.append(("summaries", "## Recent action summaries\n\n" + "\n".join(a_lines)))

    sections.append(("git_log", "## Last commits\n\n```\n" + "\n".join(ctx.git_log_lines) + "\n```"))
    sections.append(("git_status", "## Working tree status\n\n```\n" + (ctx.git_status or "(clean)") + "\n```"))
    sections.append(("tree", "## Tree\n\n```\n" + ctx.tree + "\n```"))

    if ctx.relevant_files:
        parts = ["## Relevant files\n"]
        for path, content in ctx.relevant_files.items():
            parts.append(f"### `{path}`\n\n```\n{content}\n```\n")
        sections.append(("files", "\n".join(parts)))

    # Budget: drop tree, then summaries, then git_log if over budget.
    drop_order = ["tree", "summaries", "git_log"]
    while True:
        rendered = "\n\n".join(text for _, text in sections)
        if len(rendered.split()) <= word_limit:
            return rendered
        if not drop_order:
            return rendered  # nothing else to drop; return as-is
        drop = drop_order.pop(0)
        sections = [(k, v) for k, v in sections if k != drop]
