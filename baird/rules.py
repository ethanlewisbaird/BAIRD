"""Rule engine — Phase 2 design.

Rules are declared in `.baird/project.yaml` (see `project_yaml.Rule`). Each rule
has a `check` field naming a checker function shipped here. Checkers receive a
`RuleContext` describing the action and project state; they return a `RuleResult`
with status (`pass` / `warn` / `block`) plus a message.

Pre-execution checks fire before the harness runs an action; post-execution
checks fire immediately after the action completes; on-review checks fire when
the agent self-reviews or a PR is opened.

The engine is intentionally narrow — adding a new built-in checker is just a
new function plus a decorator. User-defined Python checkers come later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .project_yaml import ProjectYaml, Rule


@dataclass
class RuleContext:
    """Everything a checker might need about a single action."""

    project: ProjectYaml
    project_root: Path | None = None
    # Pre-execution context — populated for pre_execution checks.
    command: str | None = None
    tool_name: str | None = None
    # Post-execution context — populated for post_execution checks.
    exit_code: int | None = None
    output_files: list[Path] = field(default_factory=list)
    stdout: str | None = None
    stderr: str | None = None


@dataclass
class RuleResult:
    rule_id: str
    status: str  # "pass" | "warn" | "block"
    message: str


CheckerFn = Callable[[RuleContext, Rule], RuleResult]
_CHECKERS: dict[str, CheckerFn] = {}


def register_checker(name: str) -> Callable[[CheckerFn], CheckerFn]:
    def deco(fn: CheckerFn) -> CheckerFn:
        _CHECKERS[name] = fn
        return fn

    return deco


def get_checker(name: str) -> CheckerFn | None:
    return _CHECKERS.get(name)


# ----- Built-in checkers --------------------------------------------------


@register_checker("seeds_set")
def _check_seeds_set(ctx: RuleContext, rule: Rule) -> RuleResult:
    """For known seed-bearing tools, require an explicit seed in the command."""
    cmd = ctx.command or ""
    if not cmd:
        return RuleResult(rule.id, "pass", "no command to check")

    triggers = rule.params.get("triggers", [
        "scanpy", "sklearn", "random.seed", "set.seed", "torch.manual_seed",
        "umap", "tsne", "leiden", "louvain",
    ])
    needs_seed = any(t in cmd for t in triggers)
    if not needs_seed:
        return RuleResult(rule.id, "pass", "no seed-bearing tool detected")

    seed_markers = ["--seed", "-seed=", "random_state=", "seed=", "set.seed("]
    if any(m in cmd for m in seed_markers):
        return RuleResult(rule.id, "pass", "seed present")
    return RuleResult(
        rule.id, rule.severity, "seed-bearing tool invoked without an explicit seed"
    )


@register_checker("env_pinned")
def _check_env_pinned(ctx: RuleContext, rule: Rule) -> RuleResult:
    """Project must declare an environment file at its root."""
    root = ctx.project_root
    if root is None:
        return RuleResult(rule.id, "pass", "no project root to check")

    candidates = ["environment.yml", "environment.yaml", "Dockerfile", "renv.lock", "pyproject.toml"]
    present = [c for c in candidates if (root / c).exists()]
    sif_files = list(root.glob("*.sif"))
    if present or sif_files:
        return RuleResult(rule.id, "pass", f"env spec present: {present + [p.name for p in sif_files]}")
    return RuleResult(rule.id, rule.severity, "no env spec (environment.yml / Dockerfile / *.sif / renv.lock) in project root")


@register_checker("readme_present")
def _check_readme_present(ctx: RuleContext, rule: Rule) -> RuleResult:
    root = ctx.project_root
    if root is None:
        return RuleResult(rule.id, "pass", "no project root to check")
    for name in ("README.md", "README.rst", "README.txt", "README"):
        if (root / name).exists():
            return RuleResult(rule.id, "pass", f"{name} present")
    return RuleResult(rule.id, rule.severity, "no README in project root")


@register_checker("ai_friendly_outputs")
def _check_ai_friendly_outputs(ctx: RuleContext, rule: Rule) -> RuleResult:
    """Each plot artifact (.pdf/.png/.svg) must have a sibling CSV/JSON/Parquet."""
    plot_exts = set(rule.params.get("plot_exts", [".pdf", ".png", ".svg"]))
    data_exts = set(rule.params.get("data_exts", [".csv", ".json", ".parquet", ".tsv"]))

    plots = [p for p in ctx.output_files if p.suffix.lower() in plot_exts]
    if not plots:
        return RuleResult(rule.id, "pass", "no plot artifacts in this action")

    missing = []
    for plot in plots:
        stem_dir = plot.parent
        # Sibling = same stem, any data ext.
        if not any((stem_dir / f"{plot.stem}{ext}").exists() for ext in data_exts):
            missing.append(plot.name)
    if missing:
        return RuleResult(
            rule.id,
            rule.severity,
            f"plot artifacts without a data sibling: {missing}",
        )
    return RuleResult(rule.id, "pass", "all plots have data siblings")


# ----- Engine entrypoints -------------------------------------------------


def _run(ctx: RuleContext, when: str) -> list[RuleResult]:
    results: list[RuleResult] = []
    for rule in ctx.project.rules:
        if rule.enforce != when:
            continue
        checker = get_checker(rule.check)
        if checker is None:
            results.append(RuleResult(rule.id, "warn", f"unknown checker '{rule.check}'"))
            continue
        try:
            results.append(checker(ctx, rule))
        except Exception as e:  # checker bugs shouldn't crash the harness
            results.append(RuleResult(rule.id, "warn", f"checker raised: {e!r}"))
    return results


def check_rules_pre(ctx: RuleContext) -> list[RuleResult]:
    return _run(ctx, "pre_execution")


def check_rules_post(ctx: RuleContext) -> list[RuleResult]:
    return _run(ctx, "post_execution")


def check_rules_review(ctx: RuleContext) -> list[RuleResult]:
    return _run(ctx, "on_review")


def has_blocker(results: list[RuleResult]) -> bool:
    return any(r.status == "block" for r in results)
