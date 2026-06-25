"""Tests for the research loop with a fake search backend."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from baird.memory_client import HubClient
from baird.model import OpenRouterClient
from baird.notifier import FakeTelegramTransport, Notifier, TelegramConfig
from baird.research import _parse_subqueries, run_research


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


def _two_call_transport(plan_subqs: list[str], synthesis: str):
    """Returns plan first, then synthesis on second call."""
    state = {"calls": 0}

    def t(_req):
        state["calls"] += 1
        if state["calls"] == 1:
            return {
                "choices": [{"message": {"content": json.dumps({"sub_queries": plan_subqs})}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "cost": 0.001},
            }
        return {
            "choices": [{"message": {"content": synthesis}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 100, "cost": 0.003},
        }

    return t


def _fake_search_factory(per_query: int = 2):
    def fake(query, n):
        return [
            {"title": f"hit-{i} for {query}", "url": f"https://x/{i}", "snippet": f"snip {i}"}
            for i in range(per_query)
        ]
    return fake


# ---- _parse_subqueries ------------------------------------------------


def test_parse_subqueries_plain() -> None:
    out = _parse_subqueries('{"sub_queries":["a","b"]}')
    assert out == ["a", "b"]


def test_parse_subqueries_fenced() -> None:
    out = _parse_subqueries('```\n{"sub_queries":["x"]}\n```')
    assert out == ["x"]


def test_parse_subqueries_invalid_returns_empty() -> None:
    assert _parse_subqueries("nope") == []


# ---- run_research ------------------------------------------------------


def test_research_loop_end_to_end(client: TestClient) -> None:
    hub = _Hub(client)
    tg = FakeTelegramTransport()
    notifier = Notifier(hub=hub, telegram=TelegramConfig(bot_token="t", chat_id="1"), transport=tg)
    model_client = OpenRouterClient(
        transport=_two_call_transport(["scrna integration recent", "harmony vs liger"], "## Bottom line\nuse harmony")
    )

    res = run_research(
        query="best scRNA-seq integration tool 2026",
        hub=hub,
        model_client=model_client,
        notifier=notifier,
        web_search=_fake_search_factory(per_query=2),
    )

    assert len(res.sub_questions) == 2
    assert len(res.hits) == 4  # 2 sub-queries × 2 results each
    assert "harmony" in res.synthesis
    assert res.cost_usd == pytest.approx(0.004)

    # Notification body holds the synthesis.
    inbox = hub.list_notifications(limit=5)
    assert any("research" in n["title"] for n in inbox)
    assert any("harmony" in (n.get("body") or "") for n in inbox)


def test_research_empty_search_returns_helpful_message(client: TestClient) -> None:
    hub = _Hub(client)
    model_client = OpenRouterClient(
        transport=_two_call_transport(["sq1"], "should never be called")
    )

    def no_results(q, n):
        return []

    res = run_research(
        query="anything",
        hub=hub,
        model_client=model_client,
        web_search=no_results,
    )
    assert res.hits == []
    assert "TAVILY_API_KEY" in res.synthesis


def test_research_falls_back_to_raw_query_when_plan_empty(client: TestClient) -> None:
    hub = _Hub(client)
    model_client = OpenRouterClient(
        transport=_two_call_transport([], "x")
    )
    res = run_research(
        query="raw fallback test",
        hub=hub,
        model_client=model_client,
        web_search=_fake_search_factory(1),
    )
    assert res.sub_questions == ["raw fallback test"]
