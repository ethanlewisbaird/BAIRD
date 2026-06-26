"""Satellite-side transport that routes OpenRouter calls through the hub."""

from __future__ import annotations

import httpx
import pytest

from baird.model import OpenRouterClient, make_hub_proxy_transport


def test_transport_targets_hub_proxy_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResp({
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        })

    monkeypatch.setattr(httpx, "post", fake_post)

    transport = make_hub_proxy_transport(
        hub_url="http://hub.example:8000",
        auth_token="tok",
        action_id="abc",
    )
    c = OpenRouterClient(transport=transport)
    c.complete(model="openrouter/owl-alpha", messages=[{"role": "user", "content": "hi"}])

    assert captured["url"] == "http://hub.example:8000/v1/proxy/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["headers"]["X-Baird-Action-Id"] == "abc"
    assert captured["json"]["model"] == "openrouter/owl-alpha"


def test_transport_without_token_omits_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["headers"] = headers
        return _FakeResp({
            "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0},
        })

    monkeypatch.setattr(httpx, "post", fake_post)

    transport = make_hub_proxy_transport(hub_url="http://hub:8000", auth_token=None)
    c = OpenRouterClient(transport=transport)
    c.complete(model="m", messages=[{"role": "user", "content": "x"}])
    assert "Authorization" not in captured["headers"]
    assert "X-Baird-Action-Id" not in captured["headers"]


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload
