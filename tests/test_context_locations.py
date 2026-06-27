"""Tests for Slice E — context loader surfaces a project's locations, and the
REPL's "active host" sticks through a turn."""

from __future__ import annotations

from unittest.mock import MagicMock

from baird.agent_tools import ToolEnv
from baird.context_loader import lite_repo_context, render_context
from baird.project_yaml import CheckoutHost, Location, ProjectYaml
from baird.slash import SlashContext, try_dispatch


def test_lite_context_pulls_locations_from_hub() -> None:
    hub = MagicMock()
    hub.list_decisions.return_value = []
    hub.list_actions.return_value = []
    hub.list_project_locations.return_value = [
        {"host": "hibu", "path": "/data/scrna", "role": "data"},
        {"host": "gpu", "path": "/scratch/scrna", "role": "compute"},
    ]
    py = ProjectYaml(id="scrna", name="scRNA")
    ctx = lite_repo_context(py, hub=hub)
    assert [loc.host for loc in ctx.locations] == ["hibu", "gpu"]


def test_lite_context_falls_back_to_yaml_when_hub_down() -> None:
    hub = MagicMock()
    hub.list_decisions.return_value = []
    hub.list_actions.return_value = []
    hub.list_project_locations.side_effect = RuntimeError("down")
    py = ProjectYaml(
        id="p", name="p",
        checkout_hosts=[CheckoutHost(host_id="laptop", path="/home/x")],
    )
    ctx = lite_repo_context(py, hub=hub)
    assert ctx.locations and ctx.locations[0].host == "laptop"


def test_render_context_lists_locations() -> None:
    py = ProjectYaml(id="p", name="P")
    from baird.context_loader import RepoContext

    ctx = RepoContext(
        project=py,
        project_root=None,
        branch=None,
        locations=[
            Location(host="hibu", path="/data", role="data"),
            Location(host="gpu", path="/scratch", role="compute"),
        ],
    )
    rendered = render_context(ctx)
    assert "## Locations" in rendered
    assert "hibu:/data" in rendered
    assert "gpu:/scratch" in rendered


def test_render_header_uses_first_location_host() -> None:
    py = ProjectYaml(id="p", name="P")
    from baird.context_loader import RepoContext

    ctx = RepoContext(
        project=py, project_root=None, branch=None,
        host_id="surface",  # hub hostname — must NOT leak into the header
        locations=[Location(host="hibu", path="/data", role="data")],
    )
    rendered = render_context(ctx)
    assert "Host: hibu" in rendered
    assert "Host: surface" not in rendered


def test_render_header_no_locations_uses_placeholder() -> None:
    """Issue 3: a project with no locations attached used to render
    `Host: surface` (the hub itself), misleading the model into thinking
    the project lived on the hub. Now we show an explicit placeholder
    that doubles as a UX hint about which slash command fixes it."""
    py = ProjectYaml(id="p", name="P")
    from baird.context_loader import NO_LOCATIONS_PLACEHOLDER, RepoContext

    ctx = RepoContext(
        project=py, project_root=None, branch=None,
        host_id="surface",
        locations=[],
    )
    rendered = render_context(ctx)
    assert NO_LOCATIONS_PLACEHOLDER in rendered
    assert "Host: surface" not in rendered
    assert "/project add-location" in rendered


# ---- active-host carry-over ------------------------------------------


class _FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read_file(self, path):
        self.calls.append(("read", path))
        return {"path": path, "content": "watch:\n  roots: []\n", "size": 1}

    def write_file(self, path, content, *, project_root=None, create_parents=True):
        self.calls.append(("write", path))
        return {"path": path, "bytes_written": len(content)}

    def run_command(self, command, *, cwd=None, project_root=None, timeout_s=30.0):
        self.calls.append(("run", command))
        return {"exit_code": 0, "stdout": "", "stderr": "", "tier": "project"}


def _slash_ctx(answers, active_host=None):
    hub = MagicMock()
    ex = _FakeExecutor()
    env = ToolEnv(hub=hub, executors={"hibu": ("u", "t"), "gpu": ("u", "t")},
                  executor_factory=lambda *_: ex)
    it = iter(answers)
    return SlashContext(
        hub=hub, env=env, input_fn=lambda _p: next(it),
        console=None, active_host=active_host,
    ), ex


def test_run_on_sets_active_host() -> None:
    ctx, _ex = _slash_ctx([])
    r = try_dispatch("run on hibu: ls", ctx)
    assert r.active_host == "hibu"


def test_host_edit_uses_active_host_when_missing() -> None:
    ctx, ex = _slash_ctx([], active_host="gpu")
    # Only path given (as kv); host should be filled from active_host.
    r = try_dispatch("host edit path=/new", ctx)
    assert r.handled and r.ok, r.output
    assert ("read", "~/.baird/host.yaml") in ex.calls
