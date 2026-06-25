"""`.baird/project.yaml` schema — the project memory file committed to each repo.

Per the Phase 2 design: project memory is enforceable spec, not just notes. The
YAML lives in the repo so it travels with the code; the hub mirrors it into the
memory DB so cross-project queries and the inbox can reference it.

Loading is round-trip safe: `save_project_yaml(load_project_yaml(p), p)` should
not change the file's meaningful content (comments are not preserved — yaml.dump
behaviour).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class Goal(BaseModel):
    id: str
    text: str
    status: str = "active"  # active | done | abandoned


class CheckoutHost(BaseModel):
    host_id: str
    path: str
    branch: str | None = None


class Rule(BaseModel):
    id: str
    description: str
    applies_to: list[str] = Field(default_factory=list)  # e.g. ["python", "**/*.py"]
    enforce: str = "pre_execution"  # pre_execution | post_execution | on_review
    check: str  # named checker, e.g. "seeds_set"
    severity: str = "warn"  # warn | block
    params: dict[str, Any] = Field(default_factory=dict)


class DataAlias(BaseModel):
    name: str
    volume: str
    path: str


class ProjectYaml(BaseModel):
    id: str
    name: str
    github: str | None = None
    context: str | None = None
    checkout_hosts: list[CheckoutHost] = Field(default_factory=list)
    goals: list[Goal] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)
    data_aliases: list[DataAlias] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)


def load_project_yaml(path: Path) -> ProjectYaml:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return ProjectYaml(**data)


def save_project_yaml(model: ProjectYaml, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(model.model_dump(mode="json"), f, sort_keys=False)


def project_yaml_template(project_id: str, name: str, github: str | None = None) -> ProjectYaml:
    """Starter template for `baird project init`."""
    return ProjectYaml(
        id=project_id,
        name=name,
        github=github,
        context=f"Project {name}. Write a short paragraph here describing what this is and why.",
        goals=[],
        state={},
        data_aliases=[],
        rules=[
            Rule(
                id="seeds-set",
                description="Random-seed CLI commands must pass a seed",
                applies_to=["python", "R"],
                enforce="pre_execution",
                check="seeds_set",
                severity="warn",
            ),
            Rule(
                id="env-pinned",
                description="Project must declare an environment (conda/docker/singularity)",
                applies_to=[],
                enforce="on_review",
                check="env_pinned",
                severity="warn",
            ),
            Rule(
                id="readme-present",
                description="Project root must contain a README.md",
                applies_to=[],
                enforce="on_review",
                check="readme_present",
                severity="warn",
            ),
            Rule(
                id="ai-friendly-outputs",
                description="Plot artifacts must be accompanied by a CSV/JSON/Parquet sibling with the underlying data",
                applies_to=["**/*.pdf", "**/*.png", "**/*.svg"],
                enforce="post_execution",
                check="ai_friendly_outputs",
                severity="warn",
            ),
        ],
    )
