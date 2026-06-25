"""Tests for the multi-turn REPL."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rich.console import Console

from baird.context_loader import RepoContext
from baird.memory_client import HubClient
from baird.model import OpenRouterClient
from baird.project_yaml import project_yaml_template
from baird.repl import ReplConfig, run_repl


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


def _ctx(tmp_path: Path) -> RepoContext:
    return RepoContext(
        project=project_yaml_template("p1", "P One"),
        project_root=tmp_path,
        branch="main",
        git_log_lines=[],
        git_status="",
        tree="",
        relevant_files={},
        decisions=[],
        action_summaries=[],
        rules_summary=[],
        host_id="testhost",
    )


def _model(*replies: str):
    counter = {"i": 0}
    def t(_req):
        i = counter["i"]
        counter["i"] += 1
        reply = replies[i] if i < len(replies) else "fallback"
        return {
            "choices": [{"message": {"content": reply}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0001},
        }
    return OpenRouterClient(transport=t)


def test_repl_runs_one_turn_and_exits(tmp_path: Path, client: TestClient) -> None:
    hub = _Hub(client)
    model_client = _model("hello there")
    console = Console(record=True, width=120)
    stats = run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=model_client,
        config=ReplConfig(project_id="p1"),
        console=console,
        inputs=["hi", "/exit"],
    )
    assert stats.turns == 1
    assert "hello there" in console.export_text()


def test_repl_persists_history_across_turns(tmp_path: Path, client: TestClient) -> None:
    """Each turn re-loads the session's full message history and sends it to the model.
    With two turns there should be 4 messages in the session afterward
    (user1, asst1, user2, asst2)."""
    hub = _Hub(client)
    console = Console(record=True, width=120)
    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=_model("first", "second"),
        config=ReplConfig(project_id="p1"),
        console=console,
        inputs=["one", "two", "/exit"],
    )
    sessions = hub.list_sessions(task_id="repl-p1", mode="code")
    assert sessions
    msgs = hub.get_messages(sessions[0]["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["content"] == "one"
    assert msgs[1]["content"] == "first"


def test_repl_reset_creates_new_session(tmp_path: Path, client: TestClient) -> None:
    hub = _Hub(client)
    console = Console(record=True, width=120)
    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=_model("a", "b"),
        config=ReplConfig(project_id="p2"),
        console=console,
        inputs=["one", "/reset", "two", "/exit"],
    )
    sessions = hub.list_sessions(task_id="repl-p2", mode="code")
    assert len(sessions) == 2


def test_repl_cost_command_does_not_call_model(tmp_path: Path, client: TestClient) -> None:
    hub = _Hub(client)
    console = Console(record=True, width=120)
    called = {"n": 0}
    def t(_req):
        called["n"] += 1
        return {
            "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }
    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=OpenRouterClient(transport=t),
        config=ReplConfig(project_id="p3"),
        console=console,
        inputs=["/cost", "/exit"],
    )
    assert called["n"] == 0


def test_repl_records_action_per_turn(tmp_path: Path, client: TestClient) -> None:
    hub = _Hub(client)
    console = Console(record=True, width=120)
    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=_model("r1", "r2"),
        config=ReplConfig(project_id="p4"),
        console=console,
        inputs=["a", "b", "/exit"],
    )
    actions = hub.list_actions(project_id="p4")
    assert len(actions) == 2
    for a in actions:
        assert a["model_name"] == "anthropic/claude-3-haiku"
        assert a["cost_usd"] == pytest.approx(0.0001)
        assert a["exit_code"] == 0
