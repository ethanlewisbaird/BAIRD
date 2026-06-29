"""Tests for slash-command dispatch (Slice D).

The /-commands wrap agent tools and the slice-B form helper. Tests use a
MagicMock hub and a FakeExecutor (same shape as the agent_tools tests)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from baird.agent_tools import ToolEnv
from baird.slash import (
    SlashContext,
    parse_inline_args,
    parse_kv_args,
    try_dispatch,
)


@pytest.fixture(autouse=True)
def _stub_satellite_registry(monkeypatch):
    """Pin the satellite registry seen by /project add-location so tests
    don't pick up the dev machine's real `~/.baird/satellites.json`. The
    fixture is autouse so every test in this module sees the same set."""
    fake = {
        "workstation": {"local_fwd_port": 8766, "executor_auth_token": "tok"},
        "gpu": {"local_fwd_port": 8767, "executor_auth_token": "tok"},
        "GPU-wrkstn": {"local_fwd_port": 8768, "executor_auth_token": "tok"},
    }
    # `_resolve_satellite_host` does `from .satellite import load_registry`
    # inside the function, so patching satellite.load_registry takes effect
    # on each call.
    monkeypatch.setattr("baird.satellite.load_registry", lambda: fake)


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self): return self
    def __exit__(self, *a: Any): pass

    def read_file(self, path: str) -> dict:
        self.calls.append(("read_file", {"path": path}))
        return {"path": path, "content": "watch:\n  roots:\n    - /old\n", "size": 1}

    def write_file(
        self, path: str, content: str, *, project_root=None, create_parents=True
    ) -> dict:
        self.calls.append(("write_file", {"path": path, "content": content}))
        return {"path": path, "bytes_written": len(content)}

    def run_command(
        self, command: str, *, cwd=None, project_root=None, timeout_s=30.0
    ) -> dict:
        self.calls.append(("run_command", {"command": command}))
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "tier": "project"}


def _ctx(answers: list[str], active_host: str | None = None):
    hub = MagicMock()
    exec_ = FakeExecutor()
    env = ToolEnv(
        hub=hub,
        executors={"workstation": ("http://x", "tok"), "gpu": ("http://y", "tok")},
        executor_factory=lambda url, tok: exec_,
    )
    it = iter(answers)
    return (
        SlashContext(
            hub=hub,
            env=env,
            input_fn=lambda _p: next(it),
            console=None,
            active_host=active_host,
        ),
        hub,
        exec_,
    )


# ---- argv parsing ----------------------------------------------------


def test_parse_kv_args_splits_positional_and_kv() -> None:
    pos, kv = parse_kv_args(["scrna", "name=scRNA", "github=me/scrna"])
    assert pos == ["scrna"]
    assert kv == {"name": "scRNA", "github": "me/scrna"}


def test_parse_inline_args_generic_double_dash_flags() -> None:
    """Issue #1: `--<field> <value>` must work for ANY field, not just --parent."""
    pos, kv, err = parse_inline_args(
        ["scrna", "--name", "scRNA", "--github", "me/x", "--locations", "h:/p"]
    )
    assert err is None
    assert pos == ["scrna"]
    assert kv == {"name": "scRNA", "github": "me/x", "locations": "h:/p"}


def test_parse_inline_args_equals_form() -> None:
    pos, kv, err = parse_inline_args(["p", "--name=Spatial", "--parent=umbrella"])
    assert err is None
    assert pos == ["p"]
    assert kv == {"name": "Spatial", "parent": "umbrella"}


def test_parse_inline_args_rejects_flag_value() -> None:
    pos, kv, err = parse_inline_args(["--name", "--locations"])
    assert err is not None and "flag-looking value" in err


def test_parse_inline_args_rejects_dangling_flag() -> None:
    pos, kv, err = parse_inline_args(["scrna", "--name"])
    assert err is not None and "missing a value" in err


# ---- /project new — generic --<field> support (issue #1) ----------------


def test_project_new_inline_locations_double_dash_flag() -> None:
    """The exact failing transcript from issue #1: --locations was being
    swallowed into the positional `name` slot. Must land in `locations`."""
    ctx, hub, _ = _ctx(answers=[])
    hub.list_projects.return_value = [{"id": "umbrella", "name": "S"}]
    hub.upsert_project.return_value = {"id": "umbrella-spatial"}
    hub.add_project_location.return_value = []
    r = try_dispatch(
        "project new umbrella-spatial --parent umbrella "
        "--locations GPU-wrkstn:/data-hdd0/Ethan_Baird/Dec25_xenium",
        ctx,
    )
    assert r.handled and r.ok, r.output
    hub.upsert_project.assert_called_once_with(
        id="umbrella-spatial",
        name="umbrella-spatial",
        github=None,
        parent_id="umbrella",
    )
    hub.add_project_location.assert_called_once_with(
        "umbrella-spatial",
        host="GPU-wrkstn",
        path="/data-hdd0/Ethan_Baird/Dec25_xenium",
        role=None,
    )


def test_project_new_inline_name_double_dash_flag() -> None:
    ctx, hub, _ = _ctx(answers=[])
    hub.upsert_project.return_value = {"id": "p"}
    r = try_dispatch("project new p --name 'Real Name'", ctx)
    assert r.handled and r.ok, r.output
    assert hub.upsert_project.call_args.kwargs["name"] == "Real Name"


def test_project_new_inline_github_double_dash_flag() -> None:
    ctx, hub, _ = _ctx(answers=[])
    hub.upsert_project.return_value = {"id": "p"}
    r = try_dispatch("project new p --github org/repo", ctx)
    assert r.handled and r.ok, r.output
    assert hub.upsert_project.call_args.kwargs["github"] == "org/repo"


def test_project_add_location_double_dash_flags() -> None:
    """The generic flag parser must also serve /project add-location."""
    ctx, hub, _ = _ctx(answers=[])
    hub.add_project_location.return_value = [
        {"host": "workstation", "path": "/data", "role": "data"}
    ]
    r = try_dispatch(
        "project add-location scrna --host workstation --path /data --role data", ctx
    )
    assert r.handled and r.ok, r.output
    hub.add_project_location.assert_called_once_with(
        "scrna", host="workstation", path="/data", role="data"
    )


# ---- /project new ----------------------------------------------------


def test_project_new_inline_args_skips_prompts() -> None:
    ctx, hub, _ = _ctx(answers=[])  # no prompts expected
    r = try_dispatch("project new scrna name=scRNA github=me/scrna", ctx)
    assert r is not None and r.handled and r.ok
    hub.upsert_project.assert_called_once_with(
        id="scrna", name="scRNA", github="me/scrna", parent_id=None
    )


def test_project_new_prompts_for_missing_id() -> None:
    ctx, hub, _ = _ctx(answers=["scrna"])  # id prompted
    r = try_dispatch("project new", ctx)
    assert r.handled and r.ok
    hub.upsert_project.assert_called_once()
    assert hub.upsert_project.call_args.kwargs["id"] == "scrna"


def test_project_new_inline_locations_get_added() -> None:
    ctx, hub, _ = _ctx(answers=[])
    hub.upsert_project.return_value = {"id": "scrna"}
    hub.add_project_location.return_value = []
    r = try_dispatch(
        "project new scrna locations=workstation:/data/scrna,gpu:/scratch/scrna", ctx
    )
    assert r.handled and r.ok, r.output
    assert hub.add_project_location.call_count == 2
    calls = [c.kwargs for c in hub.add_project_location.call_args_list]
    hosts = [(c["host"], c["path"]) for c in calls]
    assert hosts == [("workstation", "/data/scrna"), ("gpu", "/scratch/scrna")]
    assert "2 location(s)" in r.output


def test_project_new_skips_location_calls_when_empty() -> None:
    # locations is optional → no prompt; nothing should be sent to the hub.
    ctx, hub, _ = _ctx(answers=["scrna"])
    hub.upsert_project.return_value = {"id": "scrna"}
    r = try_dispatch("project new", ctx)
    assert r.handled and r.ok, r.output
    hub.add_project_location.assert_not_called()


def test_project_new_skips_malformed_location_entries() -> None:
    ctx, hub, _ = _ctx(answers=[])
    hub.upsert_project.return_value = {"id": "p"}
    hub.add_project_location.return_value = []
    # Mixed valid + malformed entries — malformed silently skipped.
    r = try_dispatch("project new p locations=workstation:/data,no-colon,:/no-host,gpu:", ctx)
    assert r.handled and r.ok, r.output
    assert hub.add_project_location.call_count == 1


# ---- /project add-location ------------------------------------------


def test_project_add_location_full_positional() -> None:
    ctx, hub, _ = _ctx(answers=[])
    hub.add_project_location.return_value = [{"host": "workstation", "path": "/data", "role": "data"}]
    r = try_dispatch("project add-location scrna workstation /data data", ctx)
    assert r.handled and r.ok
    hub.add_project_location.assert_called_once_with(
        "scrna", host="workstation", path="/data", role="data"
    )


def test_project_add_location_rejects_unknown_host_with_suggestion() -> None:
    """Issue 4: free-text host like 'GPU workstation' must be rejected
    rather than silently stored and broken downstream. The error should
    name the enrolled hosts AND suggest the closest match."""
    ctx, hub, _ = _ctx(answers=[])
    r = try_dispatch("project add-location scrna 'GPU workstation' /data", ctx)
    assert r.handled and not r.ok
    assert "unknown host" in r.output.lower()
    assert "GPU-wrkstn" in r.output  # closest match suggested + listed
    hub.add_project_location.assert_not_called()


def test_project_add_location_canonicalises_host_casing() -> None:
    """The stored value must match the registry's casing so downstream
    lookups by host_id succeed; lookups themselves are case-insensitive."""
    ctx, hub, _ = _ctx(answers=[])
    hub.add_project_location.return_value = [
        {"host": "GPU-wrkstn", "path": "/data", "role": None}
    ]
    r = try_dispatch("project add-location scrna gpu-WRKSTN /data", ctx)
    assert r.handled and r.ok, r.output
    args = hub.add_project_location.call_args.kwargs
    assert args["host"] == "GPU-wrkstn"


def test_project_add_location_validates_absolute_path() -> None:
    # First answer "relative" fails, second "/abs" passes.
    ctx, hub, _ = _ctx(answers=["relative", "/abs"])
    hub.add_project_location.return_value = [{"host": "h", "path": "/abs", "role": None}]
    r = try_dispatch("project add-location scrna workstation", ctx)
    assert r.handled and r.ok
    args = hub.add_project_location.call_args
    assert args.kwargs["path"] == "/abs"


# ---- /host edit -----------------------------------------------------


def test_host_edit_sets_watch_root() -> None:
    ctx, _hub, exec_ = _ctx(answers=[])
    r = try_dispatch("host edit workstation /new/root", ctx)
    assert r.handled and r.ok, r.output
    kinds = [c[0] for c in exec_.calls]
    assert kinds == ["read_file", "write_file", "run_command"]
    assert "/new/root" in exec_.calls[1][1]["content"]


# ---- /where ---------------------------------------------------------


def test_where_routes_query() -> None:
    ctx, hub, _ = _ctx(answers=[])
    ctx.env.project_id = "scrna"
    hub.get_project.return_value = {"id": "scrna", "config": {"data_aliases": []}}
    hub.list_project_locations.return_value = [
        {"host": "workstation", "path": "/data/scrna", "role": "data"},
    ]
    r = try_dispatch("where scrna", ctx)
    assert r.handled and r.ok


# ---- /run on <host>: <cmd> ------------------------------------------


def test_run_on_colon_syntax() -> None:
    ctx, _hub, exec_ = _ctx(answers=[])
    r = try_dispatch("run on workstation: ls /data", ctx)
    assert r.handled and r.ok, r.output
    assert exec_.calls == [("run_command", {"command": "ls /data"})]
    assert r.active_host == "workstation"


def test_run_uses_active_host_when_no_inline_host() -> None:
    ctx, _hub, exec_ = _ctx(answers=[], active_host="gpu")
    r = try_dispatch("run : ls /scratch", ctx)
    assert r.handled and r.ok
    assert exec_.calls == [("run_command", {"command": "ls /scratch"})]


def test_run_on_falls_through_form_when_missing_command() -> None:
    # User typed "/run on workstation" with no command — expect a form prompt.
    ctx, _hub, exec_ = _ctx(answers=["pwd"])
    r = try_dispatch("run on workstation", ctx)
    assert r.handled and r.ok
    assert exec_.calls == [("run_command", {"command": "pwd"})]


def test_run_on_destructive_command_prompts_and_can_cancel() -> None:
    ctx, _hub, exec_ = _ctx(answers=["n"])  # decline destructive prompt
    r = try_dispatch("run on workstation: pip install foo", ctx)
    assert r.handled and not r.ok
    assert "cancelled" in r.output
    assert exec_.calls == []  # never ran


# ---- /env install ---------------------------------------------------


def test_env_install_requires_explicit_confirmation() -> None:
    ctx, _hub, exec_ = _ctx(answers=["numpy\nscipy", "n"])  # env_spec then "n"
    r = try_dispatch("env install workstation scrna", ctx)
    assert r.handled and not r.ok
    assert exec_.calls == []


def test_env_install_proceeds_on_yes() -> None:
    ctx, _hub, exec_ = _ctx(answers=["numpy\nscipy", "y"])
    r = try_dispatch("env install workstation scrna", ctx)
    assert r.handled and r.ok, r.output
    # write_file (reqs.txt) + run_command (pip install)
    assert [c[0] for c in exec_.calls] == ["write_file", "run_command"]


# ---- /project new parent_id ----------------------------------------


def test_project_new_inline_parent_flag() -> None:
    """`/project new <id> --parent <pid>` shorthand."""
    ctx, hub, _ = _ctx(answers=[])
    hub.list_projects.return_value = [
        {"id": "umbrella", "name": "umbrella programme"},
    ]
    hub.upsert_project.return_value = {"id": "scrna"}
    r = try_dispatch("project new scrna --parent umbrella", ctx)
    assert r.handled and r.ok, r.output
    hub.upsert_project.assert_called_once_with(
        id="scrna", name="scrna", github=None, parent_id="umbrella"
    )


def test_project_new_parent_kv_form() -> None:
    ctx, hub, _ = _ctx(answers=[])
    hub.list_projects.return_value = [{"id": "umbrella", "name": "umbrella programme"}]
    hub.upsert_project.return_value = {"id": "scrna"}
    r = try_dispatch("project new scrna parent=umbrella", ctx)
    assert r.handled and r.ok, r.output
    assert hub.upsert_project.call_args.kwargs["parent_id"] == "umbrella"


def test_project_new_unknown_parent_suggests_closest() -> None:
    ctx, hub, _ = _ctx(answers=[])
    hub.list_projects.return_value = [{"id": "umbrella", "name": "S"}]
    r = try_dispatch("project new scrna --parent scintenel", ctx)
    assert r.handled and not r.ok
    assert "unknown parent" in r.output.lower()
    assert "umbrella" in r.output  # suggestion
    hub.upsert_project.assert_not_called()


def test_project_new_blank_parent_creates_top_level() -> None:
    ctx, hub, _ = _ctx(answers=[])
    hub.upsert_project.return_value = {"id": "p"}
    r = try_dispatch("project new p", ctx)
    assert r.handled and r.ok
    assert hub.upsert_project.call_args.kwargs["parent_id"] is None


# ---- /project tree --------------------------------------------------


def test_project_tree_renders_umbrellas_and_standalones() -> None:
    ctx, hub, _ = _ctx(answers=[])
    hub.list_projects.return_value = [
        {"id": "umbrella", "name": "umbrella programme", "parent_id": None},
        {"id": "umbrella-scrna", "name": "scRNA", "parent_id": "umbrella"},
        {"id": "umbrella-spatial", "name": "spatial", "parent_id": "umbrella"},
        {"id": "baird", "name": "BAIRD", "parent_id": None},
    ]
    r = try_dispatch("project tree", ctx)
    assert r.handled and r.ok, r.output
    out = r.output
    assert "umbrella/" in out  # umbrella marker
    assert "  umbrella-scrna" in out  # child indent
    assert "  umbrella-spatial" in out
    # Standalone root has no trailing slash.
    assert "baird  —" in out


def test_project_tree_empty() -> None:
    ctx, hub, _ = _ctx(answers=[])
    hub.list_projects.return_value = []
    r = try_dispatch("project tree", ctx)
    assert r.handled and r.ok
    assert "no projects" in r.output.lower()


# ---- /project siblings ----------------------------------------------


def test_project_siblings_lists_others_under_same_parent() -> None:
    ctx, hub, _ = _ctx(answers=[])
    ctx.env.project_id = "umbrella-scrna"
    hub.get_project.return_value = {
        "id": "umbrella-scrna",
        "parent_id": "umbrella",
    }
    hub.list_children.return_value = [
        {"id": "umbrella-scrna", "name": "scRNA"},
        {"id": "umbrella-spatial", "name": "spatial"},
        {"id": "umbrella-bulkrna", "name": "bulkRNA"},
    ]
    r = try_dispatch("project siblings", ctx)
    assert r.handled and r.ok, r.output
    assert "umbrella-spatial" in r.output
    assert "umbrella-bulkrna" in r.output
    # Self is excluded.
    assert "umbrella-scrna" not in r.output


def test_project_siblings_top_level_says_so() -> None:
    ctx, hub, _ = _ctx(answers=[])
    ctx.env.project_id = "baird"
    hub.get_project.return_value = {"id": "baird", "parent_id": None}
    r = try_dispatch("project siblings", ctx)
    assert r.handled and r.ok
    assert "no parent" in r.output.lower() or "no siblings" in r.output.lower()


# ---- Unknown command ------------------------------------------------


def test_unknown_command_returns_none() -> None:
    ctx, _hub, _exec = _ctx(answers=[])
    assert try_dispatch("nonsense", ctx) is None


# ---- /project rename (issue #3) -----------------------------------------


def test_project_rename_inline() -> None:
    ctx, hub, _ = _ctx(answers=[])
    hub.rename_project.return_value = {"id": "umbrella-spatial", "name": "Spatial"}
    r = try_dispatch("project rename umbrella-spatial Spatial", ctx)
    assert r.handled and r.ok, r.output
    hub.rename_project.assert_called_once_with("umbrella-spatial", "Spatial")
    assert "Spatial" in r.output


def test_project_rename_accepts_spaces_in_name() -> None:
    """`/project rename <id> <name with spaces>` — everything after the id
    is the name. No quoting required (issue #3)."""
    ctx, hub, _ = _ctx(answers=[])
    hub.rename_project.return_value = {
        "id": "p", "name": "Spatial Transcriptomics"
    }
    r = try_dispatch("project rename p Spatial Transcriptomics", ctx)
    assert r.handled and r.ok, r.output
    hub.rename_project.assert_called_once_with("p", "Spatial Transcriptomics")


def test_project_rename_form_prompts_for_missing() -> None:
    ctx, hub, _ = _ctx(answers=["p", "New Name"])
    hub.rename_project.return_value = {"id": "p", "name": "New Name"}
    r = try_dispatch("project rename", ctx)
    assert r.handled and r.ok
    hub.rename_project.assert_called_once_with("p", "New Name")


def test_project_rename_propagates_hub_error() -> None:
    ctx, hub, _ = _ctx(answers=[])
    hub.rename_project.side_effect = RuntimeError("404 not found")
    r = try_dispatch("project rename missing X", ctx)
    assert r.handled and not r.ok
    assert "failed to rename" in r.output


# ---- /project delete (issue #4) -----------------------------------------


def test_project_delete_confirmed() -> None:
    ctx, hub, _ = _ctx(answers=["y"])  # y to the y/N prompt
    hub.get_project.return_value = {"id": "leaf", "name": "Leaf"}
    hub.list_children.return_value = []
    hub.delete_project.return_value = {"deleted": "leaf"}
    r = try_dispatch("project delete leaf", ctx)
    assert r.handled and r.ok, r.output
    hub.delete_project.assert_called_once_with("leaf")
    assert "deleted project leaf" in r.output


def test_project_delete_aborted_on_no() -> None:
    ctx, hub, _ = _ctx(answers=["n"])
    hub.get_project.return_value = {"id": "leaf", "name": "Leaf"}
    hub.list_children.return_value = []
    r = try_dispatch("project delete leaf", ctx)
    assert r.handled and r.ok
    hub.delete_project.assert_not_called()
    assert "aborted" in r.output


def test_project_delete_blocked_when_children_exist() -> None:
    """Client refuses up-front when children are present so the user sees a
    clear message before the hub round-trip."""
    ctx, hub, _ = _ctx(answers=[])  # no prompt should fire
    hub.get_project.return_value = {"id": "umbrella", "name": "Umbrella"}
    hub.list_children.return_value = [{"id": "child-a"}, {"id": "child-b"}]
    r = try_dispatch("project delete umbrella", ctx)
    assert r.handled and not r.ok
    hub.delete_project.assert_not_called()
    assert "child-a" in r.output and "child-b" in r.output


def test_project_delete_unknown_project() -> None:
    ctx, hub, _ = _ctx(answers=[])
    hub.get_project.side_effect = RuntimeError("404")
    r = try_dispatch("project delete missing", ctx)
    assert r.handled and not r.ok
    hub.delete_project.assert_not_called()
    assert "project not found" in r.output


def test_project_delete_propagates_hub_error_on_delete() -> None:
    ctx, hub, _ = _ctx(answers=["y"])
    hub.get_project.return_value = {"id": "leaf", "name": "Leaf"}
    hub.list_children.return_value = []
    hub.delete_project.side_effect = RuntimeError("500 boom")
    r = try_dispatch("project delete leaf", ctx)
    assert r.handled and not r.ok
    assert "failed to delete" in r.output


# ---- Issue #2 guard: flag-looking values are rejected -------------------


def test_collect_form_values_rejects_flag_value_in_known() -> None:
    """The collect_form_values fallback raises FormParseError when a
    pre-supplied value starts with '--' — defends against any caller path
    that bypasses the slash parser's check."""
    from baird.tui import FormField, FormParseError, collect_form_values

    fields = [FormField("name", "name", required=True)]
    with pytest.raises(FormParseError) as ei:
        collect_form_values(
            fields, {"name": "--locations"}, input_fn=lambda _p: "x", console=None
        )
    assert "unparsed flag" in str(ei.value)


def test_project_new_rejects_dangling_flag() -> None:
    """`/project new p --locations` (no value) must error, not create."""
    ctx, hub, _ = _ctx(answers=[])
    r = try_dispatch("project new p --locations", ctx)
    assert r.handled and not r.ok
    assert "missing a value" in r.output
    hub.upsert_project.assert_not_called()


def test_project_new_rejects_flag_value() -> None:
    """`/project new --name --locations h:/p` must error, not store
    name='--locations'."""
    ctx, hub, _ = _ctx(answers=[])
    r = try_dispatch("project new p --name --locations h:/p", ctx)
    assert r.handled and not r.ok
    assert "flag-looking value" in r.output or "unparsed flag" in r.output
    hub.upsert_project.assert_not_called()


# ---- /audit-satellite ------------------------------------------------


def test_audit_satellite_inline_args_build_prompt() -> None:
    """`/audit-satellite workstation /data/raw` should produce a next_user_prompt
    naming the host + path, and NOT call any tools (the model does the scan)."""
    ctx, hub, exec_ = _ctx(answers=[])
    r = try_dispatch("audit-satellite workstation /data/raw", ctx)
    assert r.handled and r.ok
    assert r.next_user_prompt is not None
    assert "workstation" in r.next_user_prompt
    assert "/data/raw" in r.next_user_prompt
    # The slash command does NO scanning itself — the model handles it.
    assert exec_.calls == []
    # The active host should propagate so a follow-up /run picks it up.
    assert r.active_host == "workstation"


def test_audit_satellite_prompts_for_missing_host_and_path() -> None:
    """No positional args — should form-prompt host then path."""
    ctx, hub, _ = _ctx(answers=["gpu", "/scratch/proj"])
    r = try_dispatch("audit-satellite", ctx)
    assert r.handled and r.ok
    assert r.next_user_prompt is not None
    assert "gpu" in r.next_user_prompt
    assert "/scratch/proj" in r.next_user_prompt


def test_audit_satellite_uses_active_host_when_omitted() -> None:
    """If only a path is given, the active host carries over."""
    ctx, hub, _ = _ctx(answers=[], active_host="workstation")
    r = try_dispatch("audit-satellite path=/data/x", ctx)
    assert r.handled and r.ok
    assert "workstation" in (r.next_user_prompt or "")
    assert "/data/x" in (r.next_user_prompt or "")


def test_audit_satellite_rejects_relative_path() -> None:
    ctx, hub, _ = _ctx(answers=[])
    r = try_dispatch("audit-satellite workstation relative/path", ctx)
    assert r.handled and not r.ok
    assert "absolute" in r.output


def test_audit_satellite_depth_kwarg_clamped() -> None:
    """`depth=99` should be rejected (out of 1..6)."""
    ctx, hub, _ = _ctx(answers=[])
    r = try_dispatch("audit-satellite workstation /data depth=99", ctx)
    assert r.handled and not r.ok
    assert "depth" in r.output


def test_audit_satellite_depth_default_three() -> None:
    """No depth specified → depth=3 is mentioned in the prompt."""
    ctx, hub, _ = _ctx(answers=[])
    r = try_dispatch("audit-satellite workstation /data", ctx)
    assert r.handled and r.ok
    assert "max-depth 3" in (r.next_user_prompt or "")


def test_audit_satellite_registered_in_commands() -> None:
    from baird.slash import commands

    assert "audit-satellite" in commands()


# ---- /satellite enroll | list | remove --------------------------------


def test_satellite_commands_registered() -> None:
    from baird.slash import commands

    cmds = commands()
    assert "satellite enroll" in cmds
    assert "satellite list" in cmds
    assert "satellite remove" in cmds


def test_satellite_list_renders_registry(monkeypatch) -> None:
    """`/satellite list` formats the registry, no SSH involved."""
    import baird.satellite as satmod

    monkeypatch.setattr(satmod, "load_registry", lambda: {
        "workstation": {"ssh_host": "workstation", "local_fwd_port": 8766},
        "gpu": {"ssh_host": "gpu", "local_fwd_port": 8767},
    })
    monkeypatch.setattr(satmod, "tunnel_status", lambda _h: "active")
    ctx, _, _ = _ctx(answers=[])
    r = try_dispatch("satellite list", ctx)
    assert r.handled and r.ok
    assert "workstation" in r.output and "gpu" in r.output
    assert "8766" in r.output and "active" in r.output


def test_satellite_remove_drops_entry(monkeypatch) -> None:
    """`/satellite remove host_id` calls save_registry without the entry."""
    import baird.satellite as satmod

    state = {"workstation": {"ssh_host": "workstation", "local_fwd_port": 8766}}
    monkeypatch.setattr(satmod, "load_registry", lambda: dict(state))
    saved = {}
    monkeypatch.setattr(
        satmod, "save_registry", lambda reg: saved.update({"reg": reg})
    )
    ctx, _, _ = _ctx(answers=[])
    r = try_dispatch("satellite remove workstation", ctx)
    assert r.handled and r.ok
    assert "workstation" not in saved["reg"]


def test_satellite_remove_rejects_unknown(monkeypatch) -> None:
    import baird.satellite as satmod

    monkeypatch.setattr(satmod, "load_registry", lambda: {"workstation": {}})
    monkeypatch.setattr(satmod, "save_registry", lambda _r: None)
    ctx, _, _ = _ctx(answers=[])
    r = try_dispatch("satellite remove nope", ctx)
    assert r.handled and not r.ok
    assert "no satellite" in r.output


def test_satellite_enroll_invokes_enroll_and_returns_ok(monkeypatch) -> None:
    """`/satellite enroll <ssh-host>` calls baird.satellite.enroll and
    surfaces the result. We stub enroll itself so no SSH happens."""
    import baird.satellite as satmod

    called = {}

    def _fake_enroll_spec_from_local(ssh_host, *, host_id=None, git_ref="main"):
        called["spec"] = (ssh_host, host_id, git_ref)
        resolved_id = host_id or ssh_host
        spec = type("_S", (), {})()
        spec.host_id = resolved_id
        spec.ssh_host = ssh_host
        spec.local_fwd_port = None
        spec.remote_watch_root = None
        spec.use_hub_for_models = True
        return spec

    class _Res:
        host_id = "workstation"
        ssh_host = "workstation"
        remote_home = "/home/u"
        local_fwd_port = 8769
        health_ok = True
        detail = ""

    monkeypatch.setattr(satmod, "enroll_spec_from_local", _fake_enroll_spec_from_local)
    monkeypatch.setattr(satmod, "enroll", lambda _s: _Res())

    ctx, _, _ = _ctx(answers=[])
    r = try_dispatch("satellite enroll workstation", ctx)
    assert r.handled and r.ok, r.output
    assert "enrolled" in r.output and "workstation" in r.output
    assert r.active_host == "workstation"
    assert called["spec"] == ("workstation", None, "main")


def test_satellite_enroll_reports_failure(monkeypatch) -> None:
    import baird.satellite as satmod

    class _Res:
        ssh_host = "x"
        remote_home = ""
        local_fwd_port = None
        health_ok = False
        detail = "ssh: connect failed"

    monkeypatch.setattr(
        satmod, "enroll_spec_from_local",
        lambda h, **kw: type("S", (), {"host_id": h, "use_hub_for_models": True})(),
    )
    monkeypatch.setattr(satmod, "enroll", lambda _s: _Res())

    ctx, _, _ = _ctx(answers=[])
    r = try_dispatch("satellite enroll bad-host", ctx)
    assert r.handled and not r.ok
    assert "ssh: connect failed" in r.output
