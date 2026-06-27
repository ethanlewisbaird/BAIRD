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
        executors={"hibu": ("http://stub", "tok")},
        executor_factory=lambda url, tok: exec_,
    )
    return env, hub, exec_


# ---- Classifier --------------------------------------------------------


def test_run_on_destructive_command_reclassified() -> None:
    cat = build_catalogue()
    dec = classify_tool_call(cat["run_on"], {"host": "hibu", "command": "pip install foo"})
    assert dec.tier == Tier.DESTRUCTIVE


def test_run_on_safe_command_reclassified() -> None:
    cat = build_catalogue()
    dec = classify_tool_call(cat["run_on"], {"host": "hibu", "command": "ls /tmp"})
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
        {"host": "hibu", "project_id": "p", "env_spec": "numpy"},
        env,
    )
    assert not r.ok
    assert r.tier == Tier.DESTRUCTIVE


def test_dispatch_runs_tier_1(fake_env) -> None:
    env, _hub, exec_ = fake_env
    cat = build_catalogue()
    r = dispatch(cat["read_remote"], {"host": "hibu", "path": "/x"}, env)
    assert r.ok
    assert exec_.calls == [("read_file", {"path": "/x"})]


def test_dispatch_runs_tier_2_with_warning(fake_env) -> None:
    env, _hub, exec_ = fake_env
    warnings: list[str] = []
    gate = ApprovalGate(on_warn=warnings.append)
    cat = build_catalogue()
    r = dispatch(
        cat["write_remote"],
        {"host": "hibu", "path": "/tmp/in/x", "content": "x", "project_root": "/tmp"},
        env,
        gate=gate,
    )
    assert r.ok
    assert exec_.calls and exec_.calls[0][0] == "write_file"
    assert warnings, "tier-2 should emit a warning"


def test_dispatch_routes_through_explicit_host(fake_env) -> None:
    env, _hub, exec_ = fake_env
    cat = build_catalogue()
    dispatch(cat["run_on"], {"host": "hibu", "command": "ls /"}, env)
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
        {"project_id": "p", "host": "hibu", "path": "/data", "role": "data"},
        env,
    )
    hub.add_project_location.assert_called_once_with(
        "p", host="hibu", path="/data", role="data"
    )


def test_set_watch_root_round_trip(fake_env) -> None:
    env, _hub, exec_ = fake_env
    cat = build_catalogue()
    # set_watch_root is tier-2; the default gate allows it.
    r = dispatch(cat["set_watch_root"], {"host": "hibu", "path": "/projects"}, env)
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
        "config": {"data_aliases": [{"name": "raw", "volume": "hibu:/home", "path": "/data/raw"}]},
    }
    hub.list_project_locations.return_value = [
        {"host": "gpu", "path": "/scratch/p", "role": "compute"},
    ]
    cat = build_catalogue()
    r = dispatch(cat["where"], {"query": "raw"}, env)
    assert r.ok and len(r.result) == 1
    assert r.result[0]["host"] == "hibu"
