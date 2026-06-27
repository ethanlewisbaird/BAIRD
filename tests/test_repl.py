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


def test_repl_model_command_changes_model(tmp_path: Path, client: TestClient) -> None:
    hub = _Hub(client)
    console = Console(record=True, width=120)
    seen_models: list[str] = []

    def t(req):
        seen_models.append(req["body"]["model"])
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }

    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=OpenRouterClient(transport=t),
        config=ReplConfig(project_id="p4", model="anthropic/claude-3-haiku"),
        console=console,
        inputs=["one", "/model anthropic/claude-opus-4-7", "two", "/exit"],
    )
    assert seen_models == [
        "anthropic/claude-3-haiku",
        "anthropic/claude-opus-4-7",
    ]


def test_repl_model_list_then_pick_by_index(
    tmp_path: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/model` prints the catalog; `/model 2` picks #2 from it."""
    import baird.model as model_mod

    fake_catalog = [
        {"id": "anthropic/claude-opus-4-7"},
        {"id": "anthropic/claude-sonnet-4-6"},
        {"id": "openai/gpt-4o"},
    ]
    monkeypatch.setattr(
        model_mod, "top_openrouter_models", lambda n=20: fake_catalog
    )

    hub = _Hub(client)
    console = Console(record=True, width=120)
    seen_models: list[str] = []

    def t(req):
        seen_models.append(req["body"]["model"])
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }

    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=OpenRouterClient(transport=t),
        config=ReplConfig(project_id="p5", model="anthropic/claude-3-haiku"),
        console=console,
        inputs=["/model", "/model 2", "go", "/exit"],
    )

    out = console.export_text()
    assert "anthropic/claude-opus-4-7" in out
    assert "anthropic/claude-sonnet-4-6" in out
    assert seen_models == ["anthropic/claude-sonnet-4-6"]


def test_repl_multiline_input_collapses_until_close(
    tmp_path: Path, client: TestClient
) -> None:
    hub = _Hub(client)
    console = Console(record=True, width=120)
    sent: list[list[dict]] = []

    def t(req):
        sent.append(req["body"]["messages"])
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }

    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=OpenRouterClient(transport=t),
        config=ReplConfig(project_id="p6"),
        console=console,
        inputs=['"""', "line one", "line two", "line three", '"""', "/exit"],
    )
    user_msgs = [m for batch in sent for m in batch if m["role"] == "user"]
    assert user_msgs[0]["content"] == "line one\nline two\nline three"


def test_repl_session_resume_loads_existing(
    tmp_path: Path, client: TestClient
) -> None:
    """Pass session_id explicitly and the REPL attaches without creating new."""
    hub = _Hub(client)
    s = hub.new_session(mode="code", project_id="p7", task_id="repl-p7")
    # Pre-load some history.
    hub.append_message(s["id"], role="user", content="prior turn")
    hub.append_message(s["id"], role="assistant", content="prior reply")

    sent: list[list[dict]] = []

    def t(req):
        sent.append(req["body"]["messages"])
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }

    console = Console(record=True, width=120)
    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=OpenRouterClient(transport=t),
        config=ReplConfig(project_id="p7"),
        console=console,
        inputs=["new turn", "/exit"],
        session_id=s["id"],
    )
    # Prior history should be in the messages sent.
    sent_contents = [m["content"] for batch in sent for m in batch]
    assert "prior turn" in sent_contents
    assert "prior reply" in sent_contents


def test_repl_records_action_per_turn(tmp_path: Path, client: TestClient) -> None:
    hub = _Hub(client)
    console = Console(record=True, width=120)
    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=_model("r1", "r2"),
        config=ReplConfig(project_id="p4", model="anthropic/claude-3-haiku"),
        console=console,
        inputs=["a", "b", "/exit"],
    )
    actions = hub.list_actions(project_id="p4")
    assert len(actions) == 2
    for a in actions:
        assert a["model_name"] == "anthropic/claude-3-haiku"
        assert a["cost_usd"] == pytest.approx(0.0001)
        assert a["exit_code"] == 0
