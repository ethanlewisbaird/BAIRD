"""Tests for the .baird/project.yaml schema + helpers."""

from __future__ import annotations

from pathlib import Path

from baird.project_yaml import (
    Goal,
    ProjectYaml,
    Rule,
    load_project_yaml,
    project_yaml_template,
    save_project_yaml,
)


def test_template_has_default_rules() -> None:
    py = project_yaml_template("p1", "My Project", github="me/p1")
    assert py.id == "p1"
    assert py.name == "My Project"
    assert py.github == "me/p1"
    rule_ids = {r.id for r in py.rules}
    assert {"seeds-set", "env-pinned", "readme-present", "ai-friendly-outputs"} <= rule_ids


def test_round_trip(tmp_path: Path) -> None:
    py = project_yaml_template("my-project", "scRNA pipeline")
    py.goals.append(Goal(id="g1", text="integrate 3 datasets"))
    py.state = {"phase": "qc"}
    path = tmp_path / ".baird" / "project.yaml"
    save_project_yaml(py, path)

    loaded = load_project_yaml(path)
    assert loaded.id == "my-project"
    assert loaded.goals[0].text == "integrate 3 datasets"
    assert loaded.state == {"phase": "qc"}
    assert {r.id for r in loaded.rules} == {r.id for r in py.rules}


def test_rule_extra_params_round_trip(tmp_path: Path) -> None:
    py = ProjectYaml(
        id="p",
        name="p",
        rules=[
            Rule(
                id="custom",
                description="x",
                check="seeds_set",
                params={"triggers": ["my_tool"]},
            )
        ],
    )
    path = tmp_path / "project.yaml"
    save_project_yaml(py, path)
    loaded = load_project_yaml(path)
    assert loaded.rules[0].params == {"triggers": ["my_tool"]}
