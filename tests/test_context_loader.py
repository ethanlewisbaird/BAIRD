"""Tests for the repo context loader."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from baird.context_loader import (
    TREE_COLLAPSE_THRESHOLD,
    _build_tree,
    load_repo_context,
    render_context,
)
from baird.project_yaml import project_yaml_template, save_project_yaml


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "p"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.test")
    _git(root, "config", "user.name", "tester")
    save_project_yaml(project_yaml_template("p1", "P One"), root / ".baird" / "project.yaml")
    (root / "README.md").write_text("# P One\n")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("def main(): ...\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_load_offline(project: Path) -> None:
    ctx = load_repo_context(project, hub=None)
    assert ctx.project.id == "p1"
    assert ctx.branch is not None  # depends on git default, but should not be None
    assert ".baird/project.yaml" in ctx.relevant_files
    assert "README.md" in ctx.relevant_files
    assert "main.py" in ctx.tree
    assert any("init" in line for line in ctx.git_log_lines)
    # Default project has 4 rules from the template.
    assert len(ctx.rules_summary) == 4


def test_extra_file_path_scope(project: Path, tmp_path: Path) -> None:
    """Refuses to read files outside the project root via --file."""
    # Place a sneaky file outside the project but inside tmp_path.
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    ctx = load_repo_context(project, hub=None, extra_files=["../secret.txt"])
    assert "../secret.txt" not in ctx.relevant_files


def test_render_outputs_markdown(project: Path) -> None:
    ctx = load_repo_context(project, hub=None)
    rendered = render_context(ctx, token_budget=10_000)
    assert "# Project: P One" in rendered
    assert "## Tree" in rendered
    assert "## Working tree status" in rendered
    assert "## Relevant files" in rendered


def test_render_drops_sections_under_budget(project: Path) -> None:
    ctx = load_repo_context(project, hub=None)
    # Make the relevant files block huge so we go over budget.
    ctx.relevant_files["fake.txt"] = "word " * 10_000
    out = render_context(ctx, token_budget=200)  # very tight
    # The tree should be dropped first.
    assert "## Tree" not in out


def test_tree_collapses_large_dirs(tmp_path: Path) -> None:
    big = tmp_path / "bigdir"
    big.mkdir()
    for i in range(TREE_COLLAPSE_THRESHOLD + 5):
        (big / f"f{i}").write_text("x")
    out = _build_tree(tmp_path)
    assert "collapsed" in out


def test_action_summaries_filter_out_model_turns(project: Path) -> None:
    """Regression: prior REPL turns (tool_name='model') must NOT be re-injected
    as recent-action-summary bullets. They're already in session history, and
    rendering them into the system prompt causes self-mimicry — the model sees
    its own past responses (including any text-shaped tool-call attempts) and
    copies the pattern.

    Real command/tool actions (tool_name != 'model') still appear."""

    class _Hub:
        def list_decisions(self, *a, **kw): return []
        def list_actions(self, *, project_id, limit):
            return [
                {
                    "id": "a1", "tool_name": "model",
                    "summary": "The run_on tool is failing with a connection error",
                },
                {
                    "id": "a2", "tool_name": "samtools",
                    "summary": "samtools flagstat finished, mapped=92.3%",
                },
                {
                    "id": "a3", "tool_name": "model",
                    "summary": "I'll start by checking what's already registered",
                },
            ]

    ctx = load_repo_context(project, hub=_Hub())
    rendered = render_context(ctx)
    assert "samtools flagstat finished" in rendered
    assert "run_on tool is failing" not in rendered
    assert "I'll start by checking" not in rendered


def test_lite_repo_context_also_filters_model_turns() -> None:
    """The lite_repo_context path (no checkout — scratch project, /project
    switch) must filter the same way."""
    from baird.context_loader import lite_repo_context, render_context
    from baird.project_yaml import ProjectYaml

    class _Hub:
        def list_decisions(self, *a, **kw): return []
        def list_actions(self, *, project_id, limit):
            return [
                {"id": "x", "tool_name": "model", "summary": "I'll start by ..."},
                {"id": "y", "tool_name": "find", "summary": "found 17 dirs"},
            ]

    ctx = lite_repo_context(
        ProjectYaml(id="p", name="p"), hub=_Hub(), host_id="surface"
    )
    rendered = render_context(ctx)
    assert "found 17 dirs" in rendered
    assert "I'll start by" not in rendered
