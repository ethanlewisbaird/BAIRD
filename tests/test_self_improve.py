"""Tests for the self-improvement loop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baird.memory_client import HubClient
from baird.model import OpenRouterClient
from baird.notifier import FakeTelegramTransport, Notifier, TelegramConfig
from baird.self_improve import _parse_proposals, run_self_improvement


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


def _proposals_transport(proposals: list[dict]):
    def t(_req):
        return {
            "choices": [{"message": {"content": json.dumps({"proposals": proposals})}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.002},
        }
    return t


# ---- _parse_proposals --------------------------------------------------


def test_parse_proposals_plain_json() -> None:
    content = '{"proposals":[{"kind":"prompt","title":"x","rationale":"r","evidence_action_ids":[],"diff_or_text":"d"}]}'
    out = _parse_proposals(content)
    assert len(out) == 1 and out[0]["kind"] == "prompt"


def test_parse_proposals_fenced_json() -> None:
    content = '```json\n{"proposals":[{"kind":"rule","title":"r1","rationale":"","evidence_action_ids":[],"diff_or_text":""}]}\n```'
    out = _parse_proposals(content)
    assert len(out) == 1 and out[0]["kind"] == "rule"


def test_parse_proposals_malformed_returns_empty() -> None:
    assert _parse_proposals("not json at all") == []


# ---- run_self_improvement ---------------------------------------------


def test_run_self_improvement_writes_proposals_to_inbox(client: TestClient) -> None:
    hub = _Hub(client)
    # Seed at least one historical action so the corpus isn't empty.
    with hub.start_action(command="seed", host="h") as h:
        h.record_usage(cost_usd=0.001)

    proposals = [
        {
            "kind": "rule",
            "title": "add seed-set rule for sklearn",
            "rationale": "Action XYZ used sklearn without a seed.",
            "evidence_action_ids": ["abc123"],
            "diff_or_text": "id: seeds-set-sklearn\ncheck: seeds_set\n",
        },
        {
            "kind": "task",
            "title": "lower max_cost_usd on daily-poke",
            "rationale": "Cost crept up last week.",
            "evidence_action_ids": [],
            "diff_or_text": "@@ -10 +10 @@\n-max_cost_usd: 0.30\n+max_cost_usd: 0.15\n",
        },
    ]
    tg = FakeTelegramTransport()
    notifier = Notifier(hub=hub, telegram=TelegramConfig(bot_token="t", chat_id="1"), transport=tg)
    model_client = OpenRouterClient(transport=_proposals_transport(proposals))

    res = run_self_improvement(hub=hub, model_client=model_client, notifier=notifier)
    assert len(res.proposals) == 2
    assert len(res.notification_ids) == 2

    inbox = hub.list_notifications(limit=10)
    titles = {n["title"] for n in inbox}
    assert any("add seed-set" in t for t in titles)
    assert any("lower max_cost_usd" in t for t in titles)

    # Action row records cost.
    action = hub.get_action(res.action_id)
    assert action["cost_usd"] == pytest.approx(0.002)
    assert action["model_name"]
    assert action["summary"]


def test_run_self_improvement_no_proposals_writes_nothing(client: TestClient) -> None:
    hub = _Hub(client)
    tg = FakeTelegramTransport()
    notifier = Notifier(hub=hub, telegram=TelegramConfig(bot_token="t", chat_id="1"), transport=tg)
    model_client = OpenRouterClient(transport=_proposals_transport([]))

    res = run_self_improvement(hub=hub, model_client=model_client, notifier=notifier)
    assert res.proposals == []
    assert res.notification_ids == []
