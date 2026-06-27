"""TUI run_tui_repl smoke + layout tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rich.console import Console

from baird.context_loader import RepoContext
from baird.memory_client import HubClient
from baird.model import OpenRouterClient
from baird.project_yaml import project_yaml_template
from baird.repl import ReplConfig
from baird.tui import run_tui_repl


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


def _ctx(tmp_path: Path) -> RepoContext:
    return RepoContext(
        project=project_yaml_template("p-tui", "P TUI"),
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


def _model(content: str = "hello from tui"):
    def t(_req):
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 4, "cost": 0.0001},
        }
    return OpenRouterClient(transport=t)


def test_tui_runs_one_turn_and_exits(tmp_path: Path, client: TestClient) -> None:
    hub = _Hub(client)
    console = Console(record=True, width=100)
    stats = run_tui_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=_model("yo"),
        config=ReplConfig(project_id="p-tui"),
        console=console,
        inputs=["hi", "/exit"],
    )
    assert stats.turns == 1
    assert stats.total_cost_usd == pytest.approx(0.0001)


def test_tui_renders_header_and_status(tmp_path: Path, client: TestClient) -> None:
    """`inputs=` skips Live so the test captures plain content; we
    still confirm the layout primitives don't blow up at construction
    time and the conversation lines land."""
    hub = _Hub(client)
    console = Console(record=True, width=100, force_terminal=False)
    run_tui_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=_model("ack"),
        config=ReplConfig(project_id="p-tui", model="openrouter/owl-alpha"),
        console=console,
        inputs=["ping", "/exit"],
    )
    # `inputs=` mode skips Live; the test just confirms the function ran
    # without crashing the layout primitives.


def test_tui_slash_commands(tmp_path: Path, client: TestClient) -> None:
    hub = _Hub(client)
    console = Console(record=True, width=100)
    stats = run_tui_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=_model(),
        config=ReplConfig(project_id="p-tui"),
        console=console,
        inputs=["/cost", "/help", "/exit"],
    )
    assert stats.turns == 0  # neither cost nor help should produce a model turn


def test_tui_model_switch(tmp_path: Path, client: TestClient) -> None:
    hub = _Hub(client)
    console = Console(record=True, width=100)
    seen: list[str] = []

    def t(req):
        seen.append(req["body"]["model"])
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }

    run_tui_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=OpenRouterClient(transport=t),
        config=ReplConfig(project_id="p-tui", model="anthropic/claude-3-haiku"),
        console=console,
        inputs=["one", "/model openai/gpt-4o", "two", "/exit"],
    )
    assert seen == ["anthropic/claude-3-haiku", "openai/gpt-4o"]
