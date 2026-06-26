"""Hub OpenRouter proxy route."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baird import hub_proxy
from baird.config import HubConfig
from baird.hub import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    cfg = HubConfig(
        registry_db=str(tmp_path / "r.sqlite"),
        memory_db=str(tmp_path / "m.sqlite"),
    )
    return TestClient(create_app(cfg))


def test_proxy_forwards_and_returns_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def fake_forward(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return {
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "cost": 0.001},
        }

    monkeypatch.setattr(hub_proxy, "forward_call", fake_forward)

    r = client.post(
        "/v1/proxy/chat/completions",
        json={"model": "openrouter/owl-alpha", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hi"
    assert "openrouter.ai" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["body"]["model"] == "openrouter/owl-alpha"


def test_proxy_records_against_action(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        hub_proxy,
        "forward_call",
        lambda u, h, b: {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "cost": 0.005},
        },
    )

    start = client.post(
        "/actions",
        json={
            "tool_name": "model",
            "command": "chat",
            "storage_host": "test",
            "project_id": None,
            "task_id": None,
            "model_name": "openrouter/owl-alpha",
        },
    )
    assert start.status_code == 200
    action_id = start.json()["id"]

    r = client.post(
        "/v1/proxy/chat/completions",
        headers={"X-Baird-Action-Id": action_id},
        json={"model": "openrouter/owl-alpha", "messages": [{"role": "user", "content": "go"}]},
    )
    assert r.status_code == 200

    fetched = client.get(f"/actions/{action_id}").json()
    assert fetched["input_tokens"] == 10
    assert fetched["output_tokens"] == 20
    assert fetched["cost_usd"] == pytest.approx(0.005)


def test_proxy_requires_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = HubConfig(
        registry_db=str(tmp_path / "r.sqlite"),
        memory_db=str(tmp_path / "m.sqlite"),
    )
    c = TestClient(create_app(cfg))
    r = c.post(
        "/v1/proxy/chat/completions",
        json={"model": "m", "messages": []},
    )
    assert r.status_code == 500
    assert "OPENROUTER_API_KEY" in r.json()["detail"]
