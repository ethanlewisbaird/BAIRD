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
        "workstation": {
            "ssh_host": "workstation",
            "local_fwd_port": 8766,
            "executor_auth_token": "tok-workstation",
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
            return {"exit_code": 0, "stdout": "from workstation", "stderr": "", "tier": "project"}

    monkeypatch.setattr(dispatcher, "ExecutorClient", _StubClient)

    task = _task(host_id="workstation", cmd="ls /tmp")
    res = run_command_task(task, hub=_Hub(client), hub_host_id="surface")
    assert res["exit_code"] == 0
    assert res["stdout"] == "from workstation"
    assert captured["base_url"] == "http://127.0.0.1:8766"
    assert captured["token"] == "tok-workstation"
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


def test_failure_notifies_on_originating_task(client: TestClient) -> None:
    """Non-zero exit code → failure notification keyed by task_id, so a
    satellite failure surfaces in `baird inbox` next to other failures
    from the same task."""
    from baird.notifier import FakeTelegramTransport, Notifier, TelegramConfig

    transport = FakeTelegramTransport()
    notifier = Notifier(
        hub=_Hub(client),
        telegram=TelegramConfig(bot_token=None, chat_id=None),
        transport=transport,
    )
    task = _task(host_id=None, cmd="bash -c 'exit 7'")
    res = run_command_task(task, hub=_Hub(client), notifier=notifier)
    assert res["exit_code"] == 7
    rows = _Hub(client).list_notifications()
    failures = [n for n in rows if n["kind"] == "failure" and n.get("task_id") == "t1"]
    assert len(failures) == 1


def test_apply_diff_local_uses_diff_apply_to_repo(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """host_id=None → call apply_diff_to_repo directly."""
    from baird import diff_apply as _da
    from baird.dispatcher import apply_diff_anywhere

    captured: dict = {}

    class _Result:
        commit_sha = "abc123"
        files_changed = ["a.py", "b.py"]

    def fake_apply(*, repo, diff_text, commit_message, action_id=None):
        captured["repo"] = repo
        captured["msg"] = commit_message
        return _Result()

    monkeypatch.setattr(_da, "apply_diff_to_repo", fake_apply)

    out = apply_diff_anywhere(
        diff="--- a\n+++ b\n",
        commit_message="hi",
        project_root=tmp_path,
        host_id=None,
    )
    assert out["commit_sha"] == "abc123"
    assert out["files_changed"] == ["a.py", "b.py"]
    assert captured["repo"] == tmp_path


def test_apply_diff_satellite_uses_executor_client(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    (tmp_path / "satellites.json").write_text(json.dumps({
        "workstation": {"ssh_host": "workstation", "local_fwd_port": 8766, "executor_auth_token": "tok"}
    }))

    captured: dict = {}

    class _StubClient:
        def __init__(self, base_url, token, **kw):
            captured["base_url"] = base_url

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def apply_diff(self, diff, *, project_root, commit_message, **kw):
            captured["diff"] = diff
            captured["project_root"] = project_root
            captured["msg"] = commit_message
            return {"commit_sha": "remote-sha", "files_changed": ["x.py"]}

    from baird.dispatcher import apply_diff_anywhere
    monkeypatch.setattr("baird.dispatcher.ExecutorClient", _StubClient)

    out = apply_diff_anywhere(
        diff="--- a\n+++ b\n",
        commit_message="from hub",
        project_root=tmp_path / "repo",
        host_id="workstation",
    )
    assert out["commit_sha"] == "remote-sha"
    assert captured["base_url"] == "http://127.0.0.1:8766"
    assert captured["msg"] == "from hub"


def test_dispatcher_retries_on_connect_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """First two attempts hit ConnectError; third succeeds. Dispatcher
    should swallow the transients and return the eventual success."""
    import httpx

    from baird import dispatcher as disp

    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    (tmp_path / "satellites.json").write_text(json.dumps({
        "workstation": {"ssh_host": "workstation", "local_fwd_port": 8766, "executor_auth_token": "tok"}
    }))

    calls = {"n": 0}

    class _Stub:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *exc): return None

        def run_command(self, *a, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("tunnel down")
            return {"exit_code": 0, "stdout": "yay", "stderr": "", "tier": "project"}

    monkeypatch.setattr(disp, "ExecutorClient", _Stub)
    # Make backoff effectively zero in tests.
    monkeypatch.setattr(disp, "_retry", lambda op, **kw: _real_retry_no_sleep(op))

    def _real_retry_no_sleep(op):
        last = None
        for _ in range(3):
            try:
                return op()
            except Exception as e:
                last = e
        raise last  # type: ignore[misc]

    task = _task(host_id="workstation", cmd="ls")
    res = run_command_task(task, hub=_Hub(client))
    assert res["exit_code"] == 0
    assert calls["n"] == 3


def test_render_host_yaml_includes_executor_token() -> None:
    from baird.satellite import EnrollSpec, _render_host_yaml

    spec = EnrollSpec(
        ssh_host="workstation", host_id="workstation", hub_auth_token="ht",
    )
    out = _render_host_yaml(spec, remote_home="/home/user", executor_auth_token="etok")
    assert 'auth_token: "etok"' in out
    assert 'hub_auth_token: "ht"' in out
