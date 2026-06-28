"""Tests for the agent tool catalogue + dispatcher (Slice C)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from baird.agent_tools import (
    ApprovalGate,
    ToolEnv,
    build_catalogue,
    classify_tool_call,
    dispatch,
)
from baird.permissions import Tier

# ---- Fake executor + hub stubs -----------------------------------------


class FakeExecutor:
    """Stand-in for ExecutorClient. Records every call so tests can assert
    the dispatcher routed correctly."""

    def __init__(self, base_url: str = "", token: str = "") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.base_url = base_url

    def __enter__(self) -> FakeExecutor:
        return self

    def __exit__(self, *a: Any) -> None:
        pass

    def read_file(self, path: str) -> dict:
        self.calls.append(("read_file", {"path": path}))
        return {"path": path, "content": "watch:\n  roots: [/old]\n", "size": 24}

    def write_file(
        self, path: str, content: str, *, project_root=None, create_parents=True
    ) -> dict:
        self.calls.append(("write_file", {"path": path, "content": content}))
        return {"path": path, "bytes_written": len(content)}

    def run_command(
        self, command: str, *, cwd=None, project_root=None, timeout_s=30.0
    ) -> dict:
        self.calls.append(("run_command", {"command": command}))
        return {"exit_code": 0, "stdout": "", "stderr": "", "tier": "project"}

    def apply_diff(
        self, diff: str, *, project_root, commit_message,
        allow_dirty_outside_targets=True,
    ) -> dict:
        self.calls.append(("apply_diff", {"project_root": project_root}))
        return {"commit_sha": "deadbeef", "files_changed": ["a.py"]}


@pytest.fixture
def fake_env():
    hub = MagicMock()
    exec_ = FakeExecutor()
    env = ToolEnv(
        hub=hub,
        executors={"workstation": ("http://stub", "tok")},
        executor_factory=lambda url, tok: exec_,
    )
    return env, hub, exec_


# ---- Classifier --------------------------------------------------------


def test_run_on_destructive_command_reclassified() -> None:
    cat = build_catalogue()
    dec = classify_tool_call(cat["run_on"], {"host": "workstation", "command": "pip install foo"})
    assert dec.tier == Tier.DESTRUCTIVE


def test_run_on_safe_command_reclassified() -> None:
    cat = build_catalogue()
    dec = classify_tool_call(cat["run_on"], {"host": "workstation", "command": "ls /tmp"})
    assert dec.tier == Tier.SAFE


def test_write_remote_inside_root_tier_2(tmp_path) -> None:
    cat = build_catalogue()
    root = tmp_path
    target = root / "a.py"
    dec = classify_tool_call(
        cat["write_remote"],
        {"host": "h", "path": str(target), "content": "x", "project_root": str(root)},
    )
    assert dec.tier == Tier.PROJECT


def test_write_remote_outside_root_tier_3(tmp_path) -> None:
    cat = build_catalogue()
    dec = classify_tool_call(
        cat["write_remote"],
        {"host": "h", "path": "/etc/passwd", "content": "x", "project_root": str(tmp_path)},
    )
    assert dec.tier == Tier.DESTRUCTIVE


def test_install_env_always_tier_3() -> None:
    cat = build_catalogue()
    dec = classify_tool_call(
        cat["install_env"], {"host": "h", "project_id": "p", "env_spec": "numpy"}
    )
    assert dec.tier == Tier.DESTRUCTIVE


# ---- Dispatcher --------------------------------------------------------


def test_dispatch_blocks_tier_3_by_default(fake_env) -> None:
    env, _hub, _exec = fake_env
    cat = build_catalogue()
    r = dispatch(
        cat["install_env"],
        {"host": "workstation", "project_id": "p", "env_spec": "numpy"},
        env,
    )
    assert not r.ok
    assert r.tier == Tier.DESTRUCTIVE


def test_dispatch_runs_tier_1(fake_env) -> None:
    env, _hub, exec_ = fake_env
    cat = build_catalogue()
    r = dispatch(cat["read_remote"], {"host": "workstation", "path": "/x"}, env)
    assert r.ok
    assert exec_.calls == [("read_file", {"path": "/x"})]


def test_dispatch_runs_tier_2_with_warning(fake_env) -> None:
    env, _hub, exec_ = fake_env
    warnings: list[str] = []
    gate = ApprovalGate(on_warn=warnings.append)
    cat = build_catalogue()
    r = dispatch(
        cat["write_remote"],
        {"host": "workstation", "path": "/tmp/in/x", "content": "x", "project_root": "/tmp"},
        env,
        gate=gate,
    )
    assert r.ok
    assert exec_.calls and exec_.calls[0][0] == "write_file"
    assert warnings, "tier-2 should emit a warning"


def test_dispatch_routes_through_explicit_host(fake_env) -> None:
    env, _hub, exec_ = fake_env
    cat = build_catalogue()
    dispatch(cat["run_on"], {"host": "workstation", "command": "ls /"}, env)
    assert exec_.calls == [("run_command", {"command": "ls /"})]


def test_register_project_calls_hub(fake_env) -> None:
    env, hub, _exec = fake_env
    cat = build_catalogue()
    dispatch(cat["register_project"], {"id": "scrna"}, env)
    hub.upsert_project.assert_called_once_with(id="scrna", name="scrna")


def test_add_location_calls_hub(fake_env) -> None:
    env, hub, _exec = fake_env
    cat = build_catalogue()
    dispatch(
        cat["add_project_location"],
        {"project_id": "p", "host": "workstation", "path": "/data", "role": "data"},
        env,
    )
    hub.add_project_location.assert_called_once_with(
        "p", host="workstation", path="/data", role="data"
    )


def test_set_watch_root_round_trip(fake_env) -> None:
    env, _hub, exec_ = fake_env
    cat = build_catalogue()
    # set_watch_root is tier-2; the default gate allows it.
    r = dispatch(cat["set_watch_root"], {"host": "workstation", "path": "/projects"}, env)
    assert r.ok, r.error
    kinds = [c[0] for c in exec_.calls]
    assert kinds == ["read_file", "write_file", "run_command"]
    # The written body must contain the new root.
    write_args = exec_.calls[1][1]
    assert "/projects" in write_args["content"]


def test_where_uses_project_locations(fake_env) -> None:
    env, hub, _exec = fake_env
    env.project_id = "p"
    hub.get_project.return_value = {
        "id": "p",
        "config": {"data_aliases": [{"name": "raw", "volume": "workstation:/home", "path": "/data/raw"}]},
    }
    hub.list_project_locations.return_value = [
        {"host": "gpu", "path": "/scratch/p", "role": "compute"},
    ]
    cat = build_catalogue()
    r = dispatch(cat["where"], {"query": "raw"}, env)
    assert r.ok and len(r.result) == 1
    assert r.result[0]["host"] == "workstation"


# ---- Tool surface visible to the model --------------------------------


def test_add_project_location_description_includes_intent_phrases() -> None:
    """Issue 2 regression: when the user says 'location = GPU workstation
    /data/x' the model should recognise add_project_location as the right
    verb instead of falling through to a diff against project.yaml. The
    description has to spell that intent out explicitly — the tool used to
    say only 'Attach a (host, path) location to a project.' which was too
    thin for the model to match natural-language phrasing."""
    cat = build_catalogue()
    desc = cat["add_project_location"].description.lower()
    assert "where" in desc and "lives" in desc
    # A real-user example phrasing
    assert "location =" in desc
    # The anti-pattern the model fell into in production
    assert "diff" in desc and "project.yaml" in desc


def test_tool_catalogue_prompt_lists_add_project_location() -> None:
    from baird.agent_tools import tool_catalogue_prompt

    prompt = tool_catalogue_prompt()
    assert "add_project_location" in prompt
    # The description text propagates so the model sees the NL hooks.
    assert "where a project lives" in prompt.lower()
    # Required args surfaced.
    assert "project_id" in prompt and "host" in prompt and "path" in prompt


# ---- Family-aware where + list_siblings (parent/child hierarchy) -----


def test_where_expands_to_parent_and_siblings(fake_env) -> None:
    """When invoked from a child project, `where` should also search the
    parent and the sibling projects so the agent can find data from any
    assay in the same research programme."""
    env, hub, _exec = fake_env
    env.project_id = "umbrella-scrna"

    projects = {
        "umbrella-scrna": {
            "id": "umbrella-scrna",
            "parent_id": "umbrella",
            "config": {"data_aliases": []},
        },
        "umbrella": {
            "id": "umbrella",
            "parent_id": None,
            "config": {"data_aliases": [
                {"name": "manifest", "volume": "workstation:/home", "path": "/data/manifest"},
            ]},
        },
        "umbrella-spatial": {
            "id": "umbrella-spatial",
            "parent_id": "umbrella",
            "config": {"data_aliases": []},
        },
    }
    hub.get_project.side_effect = lambda pid: projects[pid]
    hub.list_children.return_value = [
        {"id": "umbrella-scrna", "name": "scRNA"},
        {"id": "umbrella-spatial", "name": "spatial"},
    ]
    locations = {
        "umbrella-scrna": [{"host": "gpu", "path": "/scratch/scrna", "role": "compute"}],
        "umbrella": [],
        "umbrella-spatial": [{"host": "workstation", "path": "/data/spatial_counts", "role": "data"}],
    }
    hub.list_project_locations.side_effect = lambda pid: locations[pid]

    cat = build_catalogue()
    # Sibling's location.
    r = dispatch(cat["where"], {"query": "spatial"}, env)
    assert r.ok, r.error
    by_pid = {h["project_id"] for h in r.result}
    assert "umbrella-spatial" in by_pid

    # Parent's alias.
    r2 = dispatch(cat["where"], {"query": "manifest"}, env)
    assert r2.ok
    assert any(h["project_id"] == "umbrella" for h in r2.result)


def test_where_top_level_project_doesnt_expand(fake_env) -> None:
    """A top-level project with no parent and no children behaves like
    before: search just its own locations + aliases."""
    env, hub, _exec = fake_env
    env.project_id = "standalone"
    hub.get_project.return_value = {
        "id": "standalone",
        "parent_id": None,
        "config": {"data_aliases": []},
    }
    hub.list_children.return_value = []
    hub.list_project_locations.return_value = [
        {"host": "workstation", "path": "/data/x", "role": "data"},
    ]
    cat = build_catalogue()
    r = dispatch(cat["where"], {"query": "data"}, env)
    assert r.ok
    assert len(r.result) == 1
    assert r.result[0]["project_id"] == "standalone"


def test_where_umbrella_expands_to_children(fake_env) -> None:
    env, hub, _exec = fake_env
    env.project_id = "umbrella"
    hub.get_project.side_effect = lambda pid: {
        "umbrella": {"id": "umbrella", "parent_id": None, "config": {}},
        "umbrella-scrna": {"id": "umbrella-scrna", "parent_id": "umbrella", "config": {}},
    }[pid]
    hub.list_children.return_value = [
        {"id": "umbrella-scrna", "name": "scRNA"},
    ]
    hub.list_project_locations.side_effect = lambda pid: {
        "umbrella": [],
        "umbrella-scrna": [{"host": "gpu", "path": "/scratch/scrna", "role": "compute"}],
    }[pid]
    cat = build_catalogue()
    r = dispatch(cat["where"], {"query": "scratch"}, env)
    assert r.ok
    assert any(h["project_id"] == "umbrella-scrna" for h in r.result)


def test_list_siblings_tool(fake_env) -> None:
    env, hub, _exec = fake_env
    env.project_id = "umbrella-scrna"
    hub.get_project.return_value = {
        "id": "umbrella-scrna", "parent_id": "umbrella",
    }
    hub.list_children.return_value = [
        {"id": "umbrella-scrna", "name": "scRNA"},
        {"id": "umbrella-spatial", "name": "spatial"},
        {"id": "umbrella-bulkrna", "name": "bulkRNA"},
    ]
    cat = build_catalogue()
    r = dispatch(cat["list_siblings"], {}, env)
    assert r.ok, r.error
    ids = sorted(s["id"] for s in r.result)
    assert ids == ["umbrella-bulkrna", "umbrella-spatial"]


def test_list_siblings_empty_for_top_level(fake_env) -> None:
    env, hub, _exec = fake_env
    env.project_id = "standalone"
    hub.get_project.return_value = {"id": "standalone", "parent_id": None}
    cat = build_catalogue()
    r = dispatch(cat["list_siblings"], {}, env)
    assert r.ok and r.result == []


def test_list_siblings_is_tier_1_safe() -> None:
    cat = build_catalogue()
    assert cat["list_siblings"].tier == Tier.SAFE


def test_where_description_mentions_family_expansion() -> None:
    cat = build_catalogue()
    desc = cat["where"].description.lower()
    assert "sibling" in desc or "umbrella" in desc or "family" in desc


def test_list_siblings_description_includes_use_case() -> None:
    cat = build_catalogue()
    desc = cat["list_siblings"].description.lower()
    assert (
        "research programme" in desc
        or "same parent" in desc
        or "umbrella" in desc
    )


def test_system_prompt_embeds_tool_catalogue() -> None:
    from baird.repl import _system_prompt

    sp = _system_prompt("## ctx\n\nminimal.")
    assert "Available hub tools" in sp
    assert "add_project_location" in sp
    assert "register_project" in sp
    # New family-aware tool is advertised too.
    assert "list_siblings" in sp
