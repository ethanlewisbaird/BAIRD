"""End-to-end tests for runner.run_task_once against a real hub + fake model."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from baird.memory_client import HubClient
from baird.model import OpenRouterClient
from baird.notifier import FakeTelegramTransport, Notifier, TelegramConfig
from baird.runner import run_task_once
from baird.tasks import IntervalTrigger, Runnable, Task


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


def _fake_transport(content: str = "done!", cost: float = 0.0123):
    def t(req: dict) -> dict:
        return {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 25, "cost": cost},
        }
    return t


def _task(task_id: str = "demo") -> Task:
    return Task(
        id=task_id,
        trigger=IntervalTrigger(interval_seconds=60),
        runnable=Runnable(prompt="ping", model="anthropic/claude-3-haiku"),
    )


def test_run_task_once_records_session_action_summary(client: TestClient) -> None:
    hub = _Hub(client)
    model_client = OpenRouterClient(transport=_fake_transport(content="hello world"))
    res = run_task_once(_task(), hub=hub, model_client=model_client, host_id="testhost")

    assert res.summary == "hello world"
    assert res.completion.usage.input_tokens == 50

    # Action was finished with cost.
    action = hub.get_action(res.action_id)
    assert action["task_id"] == "demo"
    assert action["model_name"] == "anthropic/claude-3-haiku"
    assert action["cost_usd"] == pytest.approx(0.0123)
    assert action["input_tokens"] == 50
    assert action["output_tokens"] == 25
    assert action["exit_code"] == 0
    assert action["summary"] == "hello world"
    assert action["finished_at"] is not None

    msgs = hub.get_messages(res.session_id)
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_run_task_once_notifies_on_success(client: TestClient) -> None:
    hub = _Hub(client)
    tg = FakeTelegramTransport()
    notifier = Notifier(
        hub=hub, telegram=TelegramConfig(bot_token="t", chat_id="1"), transport=tg
    )
    model_client = OpenRouterClient(transport=_fake_transport())
    run_task_once(_task(), hub=hub, model_client=model_client, notifier=notifier)

    assert any("task demo done" in text for _, text in tg.sent)


def test_run_task_once_failure_records_exit_1_and_notifies(client: TestClient) -> None:
    hub = _Hub(client)
    tg = FakeTelegramTransport()
    notifier = Notifier(
        hub=hub, telegram=TelegramConfig(bot_token="t", chat_id="1"), transport=tg
    )

    def boom(_req: dict) -> dict:
        raise RuntimeError("api down")

    model_client = OpenRouterClient(transport=boom)
    with pytest.raises(RuntimeError):
        run_task_once(_task("flaky"), hub=hub, model_client=model_client, notifier=notifier)

    # The most recent action for this task is the failed one.
    actions = hub.list_actions(task_id="flaky")
    assert actions[0]["exit_code"] == 1
    assert "api down" in (actions[0]["summary"] or "")
    assert any("flaky failed" in text for _, text in tg.sent)


def test_long_output_summary_truncated(client: TestClient) -> None:
    hub = _Hub(client)
    big = "x" * 5000
    model_client = OpenRouterClient(transport=_fake_transport(content=big))
    res = run_task_once(_task(), hub=hub, model_client=model_client)
    assert len(res.summary or "") <= 601
    assert (res.summary or "").endswith("…")
