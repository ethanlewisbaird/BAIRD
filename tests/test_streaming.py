"""SSE streaming end-to-end: client → hub proxy → upstream → back."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baird import hub_proxy
from baird.config import HubConfig
from baird.hub import create_app
from baird.model import OpenRouterClient


@pytest.fixture
def hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    cfg = HubConfig(
        registry_db=str(tmp_path / "r.sqlite"),
        memory_db=str(tmp_path / "m.sqlite"),
        recall_enabled=False,
    )
    return TestClient(create_app(cfg))


def _sse(chunks: list[dict]) -> list[bytes]:
    """Format dicts as SSE `data: …\\n` byte chunks. Adds the [DONE] marker."""
    out = []
    for c in chunks:
        out.append(f"data: {json.dumps(c)}\n".encode("utf-8"))
        out.append(b"\n")
    out.append(b"data: [DONE]\n")
    return out


def test_client_stream_complete_aggregates_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenRouterClient.stream_complete should call on_chunk per delta and
    return the full Completion."""
    captured: list[str] = []

    def fake_stream_transport(req):
        assert req["stream"] is True
        for chunk in _sse([
            {"choices": [{"delta": {"content": "Hi"}}]},
            {"choices": [{"delta": {"content": " there"}}]},
            {"choices": [{"delta": {"content": "!"}}]},
            {"usage": {"prompt_tokens": 10, "completion_tokens": 3, "cost": 0.001}},
        ]):
            text = chunk.decode("utf-8").rstrip("\n")
            if text:
                yield text

    def t(req):
        if req.get("stream"):
            return fake_stream_transport(req)
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    client = OpenRouterClient(transport=t)
    completion = client.stream_complete(
        model="m", messages=[{"role": "user", "content": "x"}],
        on_chunk=captured.append,
    )
    assert "".join(captured) == "Hi there!"
    assert completion.content == "Hi there!"
    assert completion.usage.input_tokens == 10
    assert completion.usage.output_tokens == 3


def test_hub_proxy_forwards_streaming_response(
    hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When body has stream=True, the proxy forwards SSE chunks verbatim."""
    def fake_stream(url, headers, body):
        assert body["stream"] is True
        for chunk in _sse([
            {"choices": [{"delta": {"content": "yo"}}]},
            {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0}},
        ]):
            yield chunk

    monkeypatch.setattr(hub_proxy, "stream_forward_call", fake_stream)

    with hub.stream(
        "POST",
        "/v1/proxy/chat/completions",
        json={"model": "m", "messages": [], "stream": True},
    ) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes())
    assert b"data: {" in body
    assert b"[DONE]" in body
    assert b"yo" in body


def test_hub_proxy_streaming_enriches_action(
    hub: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The streaming proxy should record cost/tokens against the action_id
    when the upstream stream carries a usage object."""
    start = hub.post(
        "/actions",
        json={
            "tool_name": "model", "command": "stream",
            "storage_host": "test",
            "project_id": None, "task_id": None,
            "model_name": "m",
        },
    )
    action_id = start.json()["id"]

    def fake_stream(url, headers, body):
        for chunk in _sse([
            {"choices": [{"delta": {"content": "hi"}}]},
            {"usage": {"prompt_tokens": 11, "completion_tokens": 4, "cost": 0.0002}},
        ]):
            yield chunk

    monkeypatch.setattr(hub_proxy, "stream_forward_call", fake_stream)

    with hub.stream(
        "POST",
        "/v1/proxy/chat/completions",
        headers={"X-Baird-Action-Id": action_id},
        json={"model": "m", "messages": [], "stream": True},
    ) as r:
        for _ in r.iter_bytes():
            pass

    fetched = hub.get(f"/actions/{action_id}").json()
    assert fetched["input_tokens"] == 11
    assert fetched["output_tokens"] == 4
    assert fetched["cost_usd"] == pytest.approx(0.0002)
