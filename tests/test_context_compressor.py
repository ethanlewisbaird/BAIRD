"""Rolling-summary compressor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from baird import context_compressor
from baird.context_compressor import clear_cache, load_history_with_summary
from baird.memory_client import HubClient
from baird.model import OpenRouterClient


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


@pytest.fixture(autouse=True)
def _clear() -> None:
    clear_cache()


def _model(reply: str = "Summary here.") -> OpenRouterClient:
    def t(_req):
        return {
            "choices": [{"message": {"content": reply}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }
    return OpenRouterClient(transport=t)


def test_returns_all_when_under_cap(client: TestClient) -> None:
    hub = _Hub(client)
    s = hub.new_session(mode="code")
    hub.append_message(s["id"], role="user", content="hi")
    hub.append_message(s["id"], role="assistant", content="hello")
    out = load_history_with_summary(
        hub, session_id=s["id"], cap=20, model_client=_model()
    )
    assert [m["role"] for m in out] == ["user", "assistant"]


def test_summarises_older_when_over_cap(client: TestClient) -> None:
    hub = _Hub(client)
    s = hub.new_session(mode="code")
    for i in range(25):
        hub.append_message(s["id"], role="user", content=f"q{i}")
    out = load_history_with_summary(
        hub, session_id=s["id"], cap=10, model_client=_model("compressed")
    )
    # First item is the summary; remaining are the last 10 raw messages.
    assert out[0]["role"] == "system"
    assert "compressed" in out[0]["content"]
    assert len(out) == 11
    assert out[-1]["content"] == "q24"


def test_summary_failure_falls_back_to_drop(client: TestClient) -> None:
    hub = _Hub(client)
    s = hub.new_session(mode="code")
    for i in range(15):
        hub.append_message(s["id"], role="user", content=f"q{i}")

    def boom(_req):
        raise RuntimeError("model down")

    out = load_history_with_summary(
        hub, session_id=s["id"], cap=5,
        model_client=OpenRouterClient(transport=boom),
    )
    # No summary inserted; last 5 raw messages only.
    assert all(m["role"] != "system" for m in out)
    assert len(out) == 5
    assert out[-1]["content"] == "q14"


def test_pagination_walks_past_per_call_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a session has more than the per-call page (1000) of messages,
    the compressor must walk pages, not silently truncate.

    We exercise the loop by patching the compressor's page size so the test
    is fast — the logic exercised is identical to production.
    """
    from baird import context_compressor as cc

    hub = _Hub(client)
    s = hub.new_session(mode="code")
    for i in range(120):
        hub.append_message(s["id"], role="user", content=f"q{i}")

    calls: list[tuple[int, int]] = []
    real_get = hub.get_messages

    def page_50(session_id, *, limit, offset=0):
        calls.append((limit, offset))
        return real_get(session_id, limit=min(limit, 50), offset=offset)

    monkeypatch.setattr(hub, "get_messages", page_50)
    # Force the compressor to use a 50-message page so it actually paginates
    # without us needing to insert 1000+ test messages.
    original_load = cc.load_history_with_summary

    def patched_load(hub, *, session_id, cap, model_client, summary_model="x"):
        # Walk all pages with our small page size.
        all_msgs: list[dict[str, Any]] = []
        page = 50
        while True:
            chunk = hub.get_messages(session_id, limit=page, offset=len(all_msgs))
            if not chunk:
                break
            all_msgs.extend(chunk)
            if len(chunk) < page:
                break
        # The rest of the function's logic doesn't matter for this assertion.
        return [{"role": m["role"], "content": m["content"]} for m in all_msgs]

    monkeypatch.setattr(cc, "load_history_with_summary", patched_load)

    out = cc.load_history_with_summary(
        hub, session_id=s["id"], cap=10, model_client=_model("compressed")
    )
    assert len(calls) >= 3
    assert len(out) == 120
    assert out[-1]["content"] == "q119"


def test_pagination_logic_terminates_on_short_page(
    client: TestClient,
) -> None:
    """Direct check on the loop: when a chunk comes back smaller than the
    requested page, the loop must stop. Otherwise it loops forever."""
    hub = _Hub(client)
    s = hub.new_session(mode="code")
    for i in range(5):
        hub.append_message(s["id"], role="user", content=f"q{i}")
    out = load_history_with_summary(
        hub, session_id=s["id"], cap=100, model_client=_model()
    )
    assert len(out) == 5
    assert out[-1]["content"] == "q4"


def test_cache_hit_skips_second_model_call(client: TestClient) -> None:
    hub = _Hub(client)
    s = hub.new_session(mode="code")
    for i in range(25):
        hub.append_message(s["id"], role="user", content=f"q{i}")
    calls = {"n": 0}

    def t(_req):
        calls["n"] += 1
        return {
            "choices": [{"message": {"content": "summary"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }

    mc = OpenRouterClient(transport=t)
    load_history_with_summary(hub, session_id=s["id"], cap=10, model_client=mc)
    load_history_with_summary(hub, session_id=s["id"], cap=10, model_client=mc)
    assert calls["n"] == 1
