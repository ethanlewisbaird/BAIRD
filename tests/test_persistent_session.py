"""Tests for the persistent per-task session + the /sessions list route."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from baird.memory_client import HubClient
from baird.model import OpenRouterClient
from baird.runner import run_task_once
from baird.tasks import IntervalTrigger, Runnable, Task


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


def _model(reply="ok"):
    def t(_req):
        return {
            "choices": [{"message": {"content": reply}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "cost": 0.0},
        }
    return OpenRouterClient(transport=t)


def test_find_or_create_returns_same_session(client: TestClient) -> None:
    hub = _Hub(client)
    a = hub.find_or_create_session_for_task(task_id="t1", mode="agent")
    b = hub.find_or_create_session_for_task(task_id="t1", mode="agent")
    assert a["id"] == b["id"]


def test_list_sessions_filters(client: TestClient) -> None:
    hub = _Hub(client)
    hub.new_session(mode="agent", task_id="A")
    hub.new_session(mode="agent", task_id="B")
    hub.new_session(mode="chat")

    only_a = hub.list_sessions(task_id="A")
    assert {s["task_id"] for s in only_a} == {"A"}
    agents = hub.list_sessions(mode="agent")
    assert all(s["mode"] == "agent" for s in agents)


def test_two_task_firings_share_session_and_accumulate_messages(client: TestClient) -> None:
    hub = _Hub(client)
    task = Task(id="cross", trigger=IntervalTrigger(interval_seconds=60), runnable=Runnable(prompt="hello"))
    run_task_once(task, hub=hub, model_client=_model("reply A"))
    run_task_once(task, hub=hub, model_client=_model("reply B"))

    sessions = hub.list_sessions(task_id="cross", mode="agent")
    assert len(sessions) == 1
    msgs = hub.get_messages(sessions[0]["id"])
    assert [m["content"] for m in msgs] == ["hello", "reply A", "hello", "reply B"]


def test_runner_passes_prior_history_to_model(client: TestClient) -> None:
    hub = _Hub(client)
    captured: list[list[dict]] = []

    def t(req):
        captured.append([dict(m) for m in req["body"]["messages"] if m["role"] != "system"])
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }

    task = Task(id="hist", trigger=IntervalTrigger(interval_seconds=60), runnable=Runnable(prompt="ping"))
    run_task_once(task, hub=hub, model_client=OpenRouterClient(transport=t))
    run_task_once(task, hub=hub, model_client=OpenRouterClient(transport=t))

    # First firing: just the new user msg. Second: prior user+assistant + new user.
    assert len(captured[0]) == 1
    assert len(captured[1]) == 3
    assert captured[1][0]["role"] == "user"
    assert captured[1][1]["role"] == "assistant"
    assert captured[1][2]["content"] == "ping"
