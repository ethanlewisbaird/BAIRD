"""Local vs. satellite command dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baird import dispatcher, satellite
from baird.dispatcher import DispatcherError, run_command_task
from baird.memory_client import HubClient
from baird.tasks import IntervalTrigger, Runnable, Task


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


def _task(host_id: str | None, cmd: str) -> Task:
    return Task(
        id="t1",
        trigger=IntervalTrigger(interval_seconds=60),
        runnable=Runnable(kind="command", host_id=host_id, args={"cmd": cmd}),
    )


def test_local_dispatch_runs_subprocess(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _task(host_id=None, cmd="echo hi")
    res = run_command_task(task, hub=_Hub(client))
    assert res["exit_code"] == 0
    assert "hi" in res["stdout"]


def test_satellite_dispatch_calls_executor_client(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    (tmp_path / "satellites.json").write_text(json.dumps({
        "hibu": {
            "ssh_host": "hibu",
            "local_fwd_port": 8766,
            "executor_auth_token": "tok-hibu",
        }
    }))

    captured: dict = {}

    class _StubClient:
        def __init__(self, base_url, token, **kw):
            captured["base_url"] = base_url
            captured["token"] = token

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def run_command(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return {"exit_code": 0, "stdout": "from hibu", "stderr": "", "tier": "project"}

    monkeypatch.setattr(dispatcher, "ExecutorClient", _StubClient)

    task = _task(host_id="hibu", cmd="ls /tmp")
    res = run_command_task(task, hub=_Hub(client), hub_host_id="surface")
    assert res["exit_code"] == 0
    assert res["stdout"] == "from hibu"
    assert captured["base_url"] == "http://127.0.0.1:8766"
    assert captured["token"] == "tok-hibu"
    assert captured["cmd"] == "ls /tmp"


def test_satellite_dispatch_missing_host_raises(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    (tmp_path / "satellites.json").write_text("{}")
    task = _task(host_id="nope", cmd="ls")
    with pytest.raises(DispatcherError, match="not in satellites.json"):
        run_command_task(task, hub=_Hub(client))


def test_satellite_dispatch_missing_token_raises(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    (tmp_path / "satellites.json").write_text(json.dumps({
        "old": {"local_fwd_port": 8770}
    }))
    task = _task(host_id="old", cmd="ls")
    with pytest.raises(DispatcherError, match="missing port or executor token"):
        run_command_task(task, hub=_Hub(client))


def test_dispatch_records_action_on_hub(client: TestClient) -> None:
    task = _task(host_id=None, cmd="echo ledger")
    hub = _Hub(client)
    res = run_command_task(task, hub=hub, hub_host_id="surface")
    action = hub.get_action(res["action_id"])
    assert action["tool_name"] == "command"
    assert action["task_id"] == "t1"
    assert action["host"] == "surface"
    assert action["exit_code"] == 0


def test_render_host_yaml_includes_executor_token() -> None:
    from baird.satellite import EnrollSpec, _render_host_yaml

    spec = EnrollSpec(
        ssh_host="hibu", host_id="hibu", hub_auth_token="ht",
    )
    out = _render_host_yaml(spec, remote_home="/home/ebaird", executor_auth_token="etok")
    assert 'auth_token: "etok"' in out
    assert 'hub_auth_token: "ht"' in out
