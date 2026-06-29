"""Repo context loader — Phase 3 design, sub-decision #2.

Builds the per-turn context block for `baird code`.

Each context source is tracked independently with a stable key and a
lightweight fingerprint for change detection. At session start, all sources
are loaded into an immutable baseline. On each subsequent turn, sources are
reconciled — unchanged sources stay in the baseline, changed sources emit a
mid-conversation update message. This avoids wasting tokens on re-rendering
content the model has already seen.

Sources:
- header (project metadata)
- context (project context paragraph + parent context)
- locations (remote locations)
- goals (project goals, including parent)
- decisions (recent hub decisions)
- rules (active rules)
- summaries (recent action summaries)
- git_log (last commits)
- git_status (working tree)
- tree (directory tree)
- files (relevant file contents)
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .memory_client import HubClient
from .project_yaml import Location, ProjectYaml, effective_locations, load_project_yaml


class ContextSource(Protocol):
    """A single contextual fact source with stable key and change detection.
    
    Each source produces:
    - `key`: stable identifier (e.g. "git_log", "tree")
    - `description`: human-readable label
    - `load()`: full rendered content
    - `fingerprint()`: lightweight comparison value (e.g. hash, or mtime)
    
    When the fingerprint changes between turns, a mid-conversation update
    message is emitted for just that source instead of regenerating the
    entire context block.
    """
    key: str
    description: str

    def load(self, ctx: RepoContext) -> str: ...
    def fingerprint(self, ctx: RepoContext) -> str: ...


@dataclass
class EpochContext:
    """Immutable baseline context plus change-detection state.
    
    Created at session start from all registered sources. On each turn,
    `reconcile()` compares current fingerprints against the stored ones
    and returns updates for any changed sources.
    """
    baseline: str
    fingerprints: dict[str, str]

    def reconcile(self, sources: list[tuple[str, ContextSource]], ctx: RepoContext) -> list[tuple[str, str]]:
        """Return [(key, rendered_content), ...] for sources whose fingerprint
        changed since the epoch was created (or last reconciled)."""
        updates: list[tuple[str, str]] = []
        for key, source in sources:
            new_fp = source.fingerprint(ctx)
            old_fp = self.fingerprints.get(key)
            if new_fp != old_fp:
                self.fingerprints[key] = new_fp
                updates.append((key, source.load(ctx)))
        return updates


# ---- Built-in context sources -------------------------------------------


class _HeaderSource:
    key = "header"
    description = "Project metadata header"

    def fingerprint(self, ctx: RepoContext) -> str:
        return f"{ctx.project.id}|{ctx.project.name}|{ctx.project.github or ''}|{ctx.branch or ''}|{project_host_for_display(ctx)}"

    def load(self, ctx: RepoContext) -> str:
        return "\n".join([
            f"# Project: {ctx.project.name} ({ctx.project.id})",
            f"Host: {project_host_for_display(ctx)}",
            f"Root: {ctx.project_root or '(no local checkout)'}",
            f"Branch: {ctx.branch or '(detached)'}",
            f"GitHub: {ctx.project.github or '(none)'}",
        ])


HEADER_SOURCE = _HeaderSource()


class _ContextSource:
    key = "context"
    description = "Project context paragraph + parent context"

    def fingerprint(self, ctx: RepoContext) -> str:
        return hashlib.md5(
            str(ctx.project.context or "").encode()
            + str(ctx.parent.context if ctx.parent else "").encode()
        ).hexdigest()

    def load(self, ctx: RepoContext) -> str:
        parts = [f"## Context\n\n{ctx.project.context or '(no context paragraph)'}"]
        if ctx.parent is not None:
            parent_lines: list[str] = [
                f"## Parent ({ctx.parent.name})",
                "",
                "*(inherited from parent — context + active goals only; "
                "rules and data_aliases stay scoped per project)*",
                "",
                ctx.parent.context or "(no parent context paragraph)",
            ]
            if ctx.parent.active_goals:
                parent_lines.append("")
                parent_lines.append("**Active goals (parent):**")
                parent_lines.extend(f"- {g}" for g in ctx.parent.active_goals)
            if ctx.parent.sibling_ids:
                parent_lines.append("")
                sibs = ", ".join(f"`{sid}` ({sname})" for sid, sname in ctx.parent.sibling_ids)
                parent_lines.append(f"**Sibling projects:** {sibs}")
            parts.append("\n".join(parent_lines))
        return "\n\n".join(parts)


CONTEXT_SOURCE = _ContextSource()


class _LocationsSource:
    key = "locations"
    description = "Remote locations for the project"

    def fingerprint(self, ctx: RepoContext) -> str:
        if not ctx.locations:
            return ""
        return ";".join(f"{l.host}:{l.path}" for l in ctx.locations)

    def load(self, ctx: RepoContext) -> str:
        if not ctx.locations:
            return ""
        loc_lines = [
            f"- `{loc.host}:{loc.path}`" + (f" — {loc.role}" if loc.role else "")
            for loc in ctx.locations
        ]
        return "## Locations\n\nProject spans these (host, path) pairs. Remote tool calls "\
               "(read_remote/write_remote/run_on/...) need a `host` argument matching one "\
               "of these host_ids:\n\n" + "\n".join(loc_lines)


LOCATIONS_SOURCE = _LocationsSource()


class _GoalsSource:
    key = "goals"
    description = "Project goals"

    def fingerprint(self, ctx: RepoContext) -> str:
        return ";".join(f"{g.text}:{g.status}" for g in ctx.project.goals)

    def load(self, ctx: RepoContext) -> str:
        goal_lines = [f"- [{g.status}] {g.text}" for g in ctx.project.goals]
        return "## Goals\n\n" + "\n".join(goal_lines)


GOALS_SOURCE = _GoalsSource()


class _DecisionsSource:
    key = "decisions"
    description = "Recent hub decisions"

    def fingerprint(self, ctx: RepoContext) -> str:
        if not ctx.decisions:
            return ""
        return hashlib.md5(str(ctx.decisions).encode()).hexdigest()

    def load(self, ctx: RepoContext) -> str:
        if not ctx.decisions:
            return ""
        d_lines = [
            f"- [{d['created_at'][:10]}] ({d['author']}) {d['text']}"
            for d in ctx.decisions
        ]
        return "## Recent decisions\n\n" + "\n".join(d_lines)


DECISIONS_SOURCE = _DecisionsSource()


class _RulesSource:
    key = "rules"
    description = "Active rules"

    def fingerprint(self, ctx: RepoContext) -> str:
        return ";".join(ctx.rules_summary)

    def load(self, ctx: RepoContext) -> str:
        if not ctx.rules_summary:
            return ""
        return "## Active rules\n\n- " + "\n- ".join(ctx.rules_summary)


RULES_SOURCE = _RulesSource()


class _SummariesSource:
    key = "summaries"
    description = "Recent action summaries"

    def fingerprint(self, ctx: RepoContext) -> str:
        if not ctx.action_summaries:
            return ""
        return hashlib.md5(str(ctx.action_summaries).encode()).hexdigest()

    def load(self, ctx: RepoContext) -> str:
        if not ctx.action_summaries:
            return ""
        a_lines = [
            f"- ({a['tool_name'] or 'cmd'}) {a['summary']}"
            for a in ctx.action_summaries
        ]
        return "## Recent action summaries\n\n" + "\n".join(a_lines)


SUMMARIES_SOURCE = _SummariesSource()


class _GitLogSource:
    key = "git_log"
    description = "Last commits"

    def fingerprint(self, ctx: RepoContext) -> str:
        return hashlib.md5("".join(ctx.git_log_lines).encode()).hexdigest()

    def load(self, ctx: RepoContext) -> str:
        return "## Last commits\n\n```\n" + "\n".join(ctx.git_log_lines) + "\n```"


GIT_LOG_SOURCE = _GitLogSource()


class _GitStatusSource:
    key = "git_status"
    description = "Working tree status"

    def fingerprint(self, ctx: RepoContext) -> str:
        return hashlib.md5(ctx.git_status.encode()).hexdigest()

    def load(self, ctx: RepoContext) -> str:
        return "## Working tree status\n\n```\n" + (ctx.git_status or "(clean)") + "\n```"


GIT_STATUS_SOURCE = _GitStatusSource()


class _TreeSource:
    key = "tree"
    description = "Directory tree"

    def fingerprint(self, ctx: RepoContext) -> str:
        return hashlib.md5(ctx.tree.encode()).hexdigest()

    def load(self, ctx: RepoContext) -> str:
        return "## Tree\n\n```\n" + ctx.tree + "\n```"


TREE_SOURCE = _TreeSource()


class _FilesSource:
    key = "files"
    description = "Relevant file contents"

    def fingerprint(self, ctx: RepoContext) -> str:
        return hashlib.md5(str(sorted(ctx.relevant_files.items())).encode()).hexdigest()

    def load(self, ctx: RepoContext) -> str:
        if not ctx.relevant_files:
            return ""
        parts = ["## Relevant files\n"]
        for path, content in ctx.relevant_files.items():
            parts.append(f"### `{path}`\n\n```\n{content}\n```\n")
        return "\n".join(parts)


FILES_SOURCE = _FilesSource()


# All context sources in render order (drop order applies to last N).
CONTEXT_SOURCES: list[tuple[str, ContextSource]] = [
    ("header", HEADER_SOURCE),
    ("context", CONTEXT_SOURCE),
    ("locations", LOCATIONS_SOURCE),
    ("goals", GOALS_SOURCE),
    ("decisions", DECISIONS_SOURCE),
    ("rules", RULES_SOURCE),
    ("summaries", SUMMARIES_SOURCE),
    ("git_log", GIT_LOG_SOURCE),
    ("git_status", GIT_STATUS_SOURCE),
    ("tree", TREE_SOURCE),
    ("files", FILES_SOURCE),
]


def build_epoch_context(ctx: RepoContext, *, token_budget: int = 6000) -> EpochContext:
    """Build an immutable baseline EpochContext from a RepoContext.
    
    Renders all registered context sources, respecting the token budget.
    Returns the baseline text plus fingerprints for change detection."""
    fingerprints: dict[str, str] = {}
    sections: list[tuple[str, str]] = []

    for key, source in CONTEXT_SOURCES:
        try:
            content = source.load(ctx)
        except Exception:
            content = ""
        if content:
            sections.append((key, content))
        fingerprints[key] = source.fingerprint(ctx)

    # Budget: drop tree, then summaries, then git_log if over budget.
    word_limit = int(token_budget * 0.75)
    drop_order = ["tree", "summaries", "git_log"]
    while True:
        baseline = "\n\n".join(text for _, text in sections)
        if len(baseline.split()) <= word_limit:
            return EpochContext(baseline=baseline, fingerprints=fingerprints)
        if not drop_order:
            return EpochContext(baseline=baseline, fingerprints=fingerprints)
        drop = drop_order.pop(0)
        sections = [(k, v) for k, v in sections if k != drop]


def reconcile_context(
    epoch: EpochContext, ctx: RepoContext,
) -> list[tuple[str, str]]:
    """Check which context sources changed and return their updated content.
    
    Returns [(key, rendered_content), ...] for changed sources. Each update
    should be injected as a mid-conversation system message so the model
    sees only the delta."""
    return epoch.reconcile(CONTEXT_SOURCES, ctx)


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
class ParentContext:
    """The inherited slice of a parent project's memory that flows into a
    child's context block. Only `context` + active `goals` flow down;
    `data_aliases` and `rules` stay scoped per-project on purpose (see
    project_baird_subprojects.md). Sibling ids are surfaced too so the
    model knows what other assays live under the same umbrella."""

    id: str
    name: str
    context: str | None
    active_goals: list[str] = field(default_factory=list)
    sibling_ids: list[tuple[str, str]] = field(default_factory=list)


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
    parent: ParentContext | None = None


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


def _load_parent_context(
    project: ProjectYaml, hub: HubClient | None
) -> ParentContext | None:
    """If `project` has a `parent_id`, fetch the parent's name + context +
    active goals + sibling ids. Returns None when there's no parent or the
    hub is unreachable. Inheritance scope: context + active goals only —
    NOT data_aliases or rules (see project_baird_subprojects.md)."""
    if hub is None or not project.parent_id:
        return None
    try:
        parent = hub.get_project(project.parent_id)
    except Exception:
        return None
    cfg = parent.get("config") or {}
    # The hub stores goals inside config (same JSON bucket as locations).
    raw_goals = cfg.get("goals") or []
    active = [
        g.get("text") or g.get("id", "")
        for g in raw_goals
        if isinstance(g, dict) and g.get("status", "active") not in {"done", "abandoned"}
    ]
    siblings: list[tuple[str, str]] = []
    try:
        for s in hub.list_children(project.parent_id):
            if s["id"] == project.id:
                continue
            siblings.append((s["id"], s.get("name") or s["id"]))
    except Exception:
        pass
    return ParentContext(
        id=parent["id"],
        name=parent.get("name") or parent["id"],
        context=parent.get("context"),
        active_goals=[g for g in active if g],
        sibling_ids=sorted(siblings),
    )


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
            # Skip `tool_name=="model"` rows — those are first-line snippets of
            # prior assistant turns. Re-injecting them into the system prompt
            # causes self-mimicry loops (the model sees its own past responses,
            # including text-shaped tool-call attempts, and copies the
            # pattern). Real command/tool actions stay.
            summaries = [
                a for a in actions
                if a.get("summary") and a.get("tool_name") != "model"
            ][:n_action_summaries]
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
        parent=_load_parent_context(project, hub),
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
            # Skip `tool_name=="model"` rows — those are first-line snippets of
            # prior assistant turns. Re-injecting them into the system prompt
            # causes self-mimicry loops (the model sees its own past responses,
            # including text-shaped tool-call attempts, and copies the
            # pattern). Real command/tool actions stay.
            summaries = [
                a for a in actions
                if a.get("summary") and a.get("tool_name") != "model"
            ][:n_action_summaries]
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
        parent=_load_parent_context(project, hub),
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
    """Render a RepoContext as a markdown block. Delegates to the epoch-based
    context source system. Drops sections in priority order if the result
    would exceed `token_budget` (approx: 1 token ≈ 0.75 words)."""
    epoch = build_epoch_context(ctx, token_budget=token_budget)
    return epoch.baseline
