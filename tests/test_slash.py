"""Tests for slash-command dispatch (Slice D).

The /-commands wrap agent tools and the slice-B form helper. Tests use a
MagicMock hub and a FakeExecutor (same shape as the agent_tools tests)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from baird.agent_tools import ToolEnv
from baird.slash import (
    SlashContext,
    parse_kv_args,
    try_dispatch,
)


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
        executors={"hibu": ("http://x", "tok"), "gpu": ("http://y", "tok")},
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


# ---- /project new ----------------------------------------------------


def test_project_new_inline_args_skips_prompts() -> None:
    ctx, hub, _ = _ctx(answers=[])  # no prompts expected
    r = try_dispatch("project new scrna name=scRNA github=me/scrna", ctx)
    assert r is not None and r.handled and r.ok
    hub.upsert_project.assert_called_once_with(id="scrna", name="scRNA", github="me/scrna")


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
        "project new scrna locations=hibu:/data/scrna,gpu:/scratch/scrna", ctx
    )
    assert r.handled and r.ok, r.output
    assert hub.add_project_location.call_count == 2
    calls = [c.kwargs for c in hub.add_project_location.call_args_list]
    hosts = [(c["host"], c["path"]) for c in calls]
    assert hosts == [("hibu", "/data/scrna"), ("gpu", "/scratch/scrna")]
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
    r = try_dispatch("project new p locations=hibu:/data,no-colon,:/no-host,gpu:", ctx)
    assert r.handled and r.ok, r.output
    assert hub.add_project_location.call_count == 1


# ---- /project add-location ------------------------------------------


def test_project_add_location_full_positional() -> None:
    ctx, hub, _ = _ctx(answers=[])
    hub.add_project_location.return_value = [{"host": "hibu", "path": "/data", "role": "data"}]
    r = try_dispatch("project add-location scrna hibu /data data", ctx)
    assert r.handled and r.ok
    hub.add_project_location.assert_called_once_with(
        "scrna", host="hibu", path="/data", role="data"
    )


def test_project_add_location_validates_absolute_path() -> None:
    # First answer "relative" fails, second "/abs" passes.
    ctx, hub, _ = _ctx(answers=["relative", "/abs"])
    hub.add_project_location.return_value = [{"host": "h", "path": "/abs", "role": None}]
    r = try_dispatch("project add-location scrna hibu", ctx)
    assert r.handled and r.ok
    args = hub.add_project_location.call_args
    assert args.kwargs["path"] == "/abs"


# ---- /host edit -----------------------------------------------------


def test_host_edit_sets_watch_root() -> None:
    ctx, _hub, exec_ = _ctx(answers=[])
    r = try_dispatch("host edit hibu /new/root", ctx)
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
        {"host": "hibu", "path": "/data/scrna", "role": "data"},
    ]
    r = try_dispatch("where scrna", ctx)
    assert r.handled and r.ok


# ---- /run on <host>: <cmd> ------------------------------------------


def test_run_on_colon_syntax() -> None:
    ctx, _hub, exec_ = _ctx(answers=[])
    r = try_dispatch("run on hibu: ls /data", ctx)
    assert r.handled and r.ok, r.output
    assert exec_.calls == [("run_command", {"command": "ls /data"})]
    assert r.active_host == "hibu"


def test_run_uses_active_host_when_no_inline_host() -> None:
    ctx, _hub, exec_ = _ctx(answers=[], active_host="gpu")
    r = try_dispatch("run : ls /scratch", ctx)
    assert r.handled and r.ok
    assert exec_.calls == [("run_command", {"command": "ls /scratch"})]


def test_run_on_falls_through_form_when_missing_command() -> None:
    # User typed "/run on hibu" with no command — expect a form prompt.
    ctx, _hub, exec_ = _ctx(answers=["pwd"])
    r = try_dispatch("run on hibu", ctx)
    assert r.handled and r.ok
    assert exec_.calls == [("run_command", {"command": "pwd"})]


def test_run_on_destructive_command_prompts_and_can_cancel() -> None:
    ctx, _hub, exec_ = _ctx(answers=["n"])  # decline destructive prompt
    r = try_dispatch("run on hibu: pip install foo", ctx)
    assert r.handled and not r.ok
    assert "cancelled" in r.output
    assert exec_.calls == []  # never ran


# ---- /env install ---------------------------------------------------


def test_env_install_requires_explicit_confirmation() -> None:
    ctx, _hub, exec_ = _ctx(answers=["numpy\nscipy", "n"])  # env_spec then "n"
    r = try_dispatch("env install hibu scrna", ctx)
    assert r.handled and not r.ok
    assert exec_.calls == []


def test_env_install_proceeds_on_yes() -> None:
    ctx, _hub, exec_ = _ctx(answers=["numpy\nscipy", "y"])
    r = try_dispatch("env install hibu scrna", ctx)
    assert r.handled and r.ok, r.output
    # write_file (reqs.txt) + run_command (pip install)
    assert [c[0] for c in exec_.calls] == ["write_file", "run_command"]


# ---- Unknown command ------------------------------------------------


def test_unknown_command_returns_none() -> None:
    ctx, _hub, _exec = _ctx(answers=[])
    assert try_dispatch("nonsense", ctx) is None
