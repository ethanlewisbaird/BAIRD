"""Tests for parent/child context inheritance (Slice C).

When the active project has a `parent_id`, the rendered context block
auto-includes the parent's `context` paragraph and its active goals
(status != done/abandoned), tagged as inherited. Sibling project ids are
listed too. `data_aliases` and `rules` deliberately do NOT inherit.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from baird.context_loader import (
    ParentContext,
    RepoContext,
    lite_repo_context,
    render_context,
)
from baird.project_yaml import ProjectYaml


def _hub_with_parent(
    parent_id: str = "umbrella",
    parent_context: str | None = "Umbrella research programme.",
    parent_goals: list[dict] | None = None,
    siblings: list[dict] | None = None,
) -> MagicMock:
    hub = MagicMock()
    hub.list_decisions.return_value = []
    hub.list_actions.return_value = []
    hub.list_project_locations.return_value = []
    hub.get_project.return_value = {
        "id": parent_id,
        "name": parent_id.upper(),
        "context": parent_context,
        "parent_id": None,
        "config": {
            "goals": parent_goals
            or [
                {"id": "g1", "text": "Cross-cohort meta-analysis", "status": "active"},
                {"id": "g2", "text": "Old goal", "status": "done"},
                {"id": "g3", "text": "Grant submission", "status": "active"},
            ],
        },
    }
    hub.list_children.return_value = siblings or [
        {"id": "umbrella-scrna", "name": "scRNA"},
        {"id": "umbrella-spatial", "name": "spatial"},
    ]
    return hub


def test_lite_context_loads_parent_when_parent_id_set() -> None:
    hub = _hub_with_parent()
    py = ProjectYaml(id="umbrella-scrna", name="scRNA", parent_id="umbrella")
    ctx = lite_repo_context(py, hub=hub)
    assert ctx.parent is not None
    assert ctx.parent.id == "umbrella"
    assert "Umbrella" in (ctx.parent.context or "")


def test_parent_active_goals_filter_done_and_abandoned() -> None:
    hub = _hub_with_parent(
        parent_goals=[
            {"id": "a", "text": "Active 1", "status": "active"},
            {"id": "b", "text": "Done one", "status": "done"},
            {"id": "c", "text": "Abandoned one", "status": "abandoned"},
            {"id": "d", "text": "Active 2", "status": "active"},
        ]
    )
    py = ProjectYaml(id="child", name="child", parent_id="umbrella")
    ctx = lite_repo_context(py, hub=hub)
    assert ctx.parent is not None
    goals = ctx.parent.active_goals
    assert "Active 1" in goals
    assert "Active 2" in goals
    assert "Done one" not in goals
    assert "Abandoned one" not in goals


def test_no_parent_loaded_when_no_parent_id() -> None:
    hub = _hub_with_parent()
    py = ProjectYaml(id="standalone", name="standalone")  # no parent_id
    ctx = lite_repo_context(py, hub=hub)
    assert ctx.parent is None
    # Parent endpoint shouldn't even be called.
    hub.get_project.assert_not_called()


def test_parent_load_failure_degrades_gracefully() -> None:
    hub = _hub_with_parent()
    hub.get_project.side_effect = RuntimeError("hub down")
    py = ProjectYaml(id="child", name="child", parent_id="umbrella")
    ctx = lite_repo_context(py, hub=hub)
    assert ctx.parent is None


def test_render_includes_parent_section_marked_inherited() -> None:
    py = ProjectYaml(id="child", name="child", parent_id="umbrella")
    ctx = RepoContext(
        project=py,
        project_root=None,
        branch=None,
        parent=ParentContext(
            id="umbrella",
            name="umbrella programme",
            context="Umbrella research programme spanning multiple assays.",
            active_goals=["Cross-cohort meta-analysis", "Grant submission"],
            sibling_ids=[("umbrella-spatial", "spatial")],
        ),
    )
    out = render_context(ctx)
    assert "## Parent (umbrella programme)" in out
    assert "inherited" in out.lower()
    assert "Umbrella research programme" in out
    assert "Cross-cohort meta-analysis" in out
    assert "Grant submission" in out
    assert "umbrella-spatial" in out


def test_render_omits_parent_section_when_top_level() -> None:
    py = ProjectYaml(id="standalone", name="standalone")
    ctx = RepoContext(project=py, project_root=None, branch=None, parent=None)
    out = render_context(ctx)
    assert "## Parent" not in out


def test_parent_inheritance_loader_drops_rules_and_aliases() -> None:
    """Spec: data_aliases and rules do NOT flow down. Verify the loader
    drops them — the ParentContext dataclass has no slots for them."""
    hub = MagicMock()
    hub.list_decisions.return_value = []
    hub.list_actions.return_value = []
    hub.list_project_locations.return_value = []
    hub.list_children.return_value = []
    hub.get_project.return_value = {
        "id": "umbrella",
        "name": "umbrella programme",
        "context": "Umbrella.",
        "parent_id": None,
        "config": {
            "goals": [{"id": "g", "text": "G", "status": "active"}],
            "data_aliases": [{"name": "raw", "volume": "h:/v", "path": "/data"}],
            "rules": [{"id": "r", "description": "secret rule", "check": "x"}],
        },
    }
    py = ProjectYaml(id="child", name="child", parent_id="umbrella")
    ctx = lite_repo_context(py, hub=hub)
    assert ctx.parent is not None
    # ParentContext dataclass only carries context + goals + siblings.
    assert not hasattr(ctx.parent, "data_aliases")
    assert not hasattr(ctx.parent, "rules")
    # The rendered block carries the parent goal but not the rule's body.
    out = render_context(ctx)
    assert "secret rule" not in out
    assert "raw" not in out  # alias name not surfaced via parent


def test_sibling_ids_exclude_self() -> None:
    hub = _hub_with_parent(
        siblings=[
            {"id": "umbrella-scrna", "name": "scRNA"},
            {"id": "umbrella-spatial", "name": "spatial"},
        ]
    )
    py = ProjectYaml(id="umbrella-scrna", name="scRNA", parent_id="umbrella")
    ctx = lite_repo_context(py, hub=hub)
    assert ctx.parent is not None
    sib_ids = [sid for sid, _ in ctx.parent.sibling_ids]
    assert "umbrella-spatial" in sib_ids
    assert "umbrella-scrna" not in sib_ids  # self excluded
