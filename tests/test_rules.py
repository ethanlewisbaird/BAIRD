"""Tests for the rule engine + built-in checkers."""

from __future__ import annotations

from pathlib import Path

from baird.project_yaml import Rule, project_yaml_template
from baird.rules import (
    RuleContext,
    RuleResult,
    check_rules_post,
    check_rules_pre,
    check_rules_review,
    has_blocker,
)


def _ctx(tmp_path: Path, **kwargs) -> RuleContext:
    py = project_yaml_template("p", "p")
    return RuleContext(project=py, project_root=tmp_path, **kwargs)


# ---- seeds_set ---------------------------------------------------------


def test_seeds_set_passes_when_no_trigger(tmp_path: Path) -> None:
    results = check_rules_pre(_ctx(tmp_path, command="echo hello"))
    seeds = [r for r in results if r.rule_id == "seeds-set"]
    assert seeds and seeds[0].status == "pass"


def test_seeds_set_warns_when_trigger_without_seed(tmp_path: Path) -> None:
    results = check_rules_pre(_ctx(tmp_path, command="python -c 'import scanpy; scanpy.tl.leiden(adata)'"))
    seeds = [r for r in results if r.rule_id == "seeds-set"]
    assert seeds and seeds[0].status == "warn"


def test_seeds_set_passes_with_explicit_seed(tmp_path: Path) -> None:
    results = check_rules_pre(_ctx(tmp_path, command="python run.py --seed 42 --umap"))
    seeds = [r for r in results if r.rule_id == "seeds-set"]
    assert seeds and seeds[0].status == "pass"


# ---- env_pinned --------------------------------------------------------


def test_env_pinned_warns_with_empty_root(tmp_path: Path) -> None:
    results = check_rules_review(_ctx(tmp_path))
    envs = [r for r in results if r.rule_id == "env-pinned"]
    assert envs and envs[0].status == "warn"


def test_env_pinned_passes_with_environment_yml(tmp_path: Path) -> None:
    (tmp_path / "environment.yml").write_text("name: x\n")
    results = check_rules_review(_ctx(tmp_path))
    envs = [r for r in results if r.rule_id == "env-pinned"]
    assert envs and envs[0].status == "pass"


def test_env_pinned_passes_with_dockerfile(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.11\n")
    results = check_rules_review(_ctx(tmp_path))
    envs = [r for r in results if r.rule_id == "env-pinned"]
    assert envs and envs[0].status == "pass"


# ---- readme_present ----------------------------------------------------


def test_readme_present_warns(tmp_path: Path) -> None:
    results = check_rules_review(_ctx(tmp_path))
    readmes = [r for r in results if r.rule_id == "readme-present"]
    assert readmes and readmes[0].status == "warn"


def test_readme_present_passes(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# hi\n")
    results = check_rules_review(_ctx(tmp_path))
    readmes = [r for r in results if r.rule_id == "readme-present"]
    assert readmes and readmes[0].status == "pass"


# ---- ai_friendly_outputs -----------------------------------------------


def test_ai_friendly_outputs_warns_when_plot_alone(tmp_path: Path) -> None:
    plot = tmp_path / "fig1.pdf"
    plot.write_bytes(b"%PDF-1.4")
    ctx = _ctx(tmp_path, output_files=[plot])
    results = check_rules_post(ctx)
    aifo = [r for r in results if r.rule_id == "ai-friendly-outputs"]
    assert aifo and aifo[0].status == "warn"


def test_ai_friendly_outputs_passes_when_csv_sibling_present(tmp_path: Path) -> None:
    plot = tmp_path / "fig1.pdf"
    plot.write_bytes(b"%PDF-1.4")
    (tmp_path / "fig1.csv").write_text("a,b\n")
    ctx = _ctx(tmp_path, output_files=[plot])
    results = check_rules_post(ctx)
    aifo = [r for r in results if r.rule_id == "ai-friendly-outputs"]
    assert aifo and aifo[0].status == "pass"


def test_ai_friendly_outputs_passes_when_no_plots(tmp_path: Path) -> None:
    only_data = tmp_path / "table.csv"
    only_data.write_text("a,b\n")
    ctx = _ctx(tmp_path, output_files=[only_data])
    results = check_rules_post(ctx)
    aifo = [r for r in results if r.rule_id == "ai-friendly-outputs"]
    assert aifo and aifo[0].status == "pass"


# ---- engine plumbing ---------------------------------------------------


def test_unknown_checker_is_warn_not_crash(tmp_path: Path) -> None:
    py = project_yaml_template("p", "p")
    py.rules.append(Rule(id="r-unknown", description="x", check="does_not_exist", enforce="pre_execution"))
    ctx = RuleContext(project=py, project_root=tmp_path, command="echo hi")
    results = check_rules_pre(ctx)
    unknown = [r for r in results if r.rule_id == "r-unknown"]
    assert unknown and unknown[0].status == "warn"
    assert "unknown checker" in unknown[0].message


def test_has_blocker() -> None:
    assert not has_blocker([RuleResult("a", "warn", "x"), RuleResult("b", "pass", "y")])
    assert has_blocker([RuleResult("a", "block", "x")])


def test_block_severity_propagates(tmp_path: Path) -> None:
    py = project_yaml_template("p", "p")
    # promote ai-friendly-outputs to a blocker for this project
    for r in py.rules:
        if r.id == "ai-friendly-outputs":
            r.severity = "block"
    plot = tmp_path / "x.pdf"
    plot.write_bytes(b"%PDF")
    ctx = RuleContext(project=py, project_root=tmp_path, output_files=[plot])
    results = check_rules_post(ctx)
    assert has_blocker(results)
