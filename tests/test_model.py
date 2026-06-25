"""Tests for the OpenRouter client with a fake transport."""

from __future__ import annotations

import pytest

from baird.model import ModelError, OpenRouterClient, Usage


def _fake_response(content: str = "hi", prompt_tokens: int = 10, completion_tokens: int = 5, cost: float | None = None) -> dict:
    usage: dict = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    if cost is not None:
        usage["cost"] = cost
    return {
        "id": "x",
        "model": "anthropic/claude-3-haiku",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": usage,
    }


def test_complete_with_fake_transport_uses_returned_cost() -> None:
    def transport(req: dict) -> dict:
        return _fake_response(content="ok", prompt_tokens=100, completion_tokens=200, cost=0.0042)

    c = OpenRouterClient(transport=transport)
    res = c.complete(model="anthropic/claude-3-haiku", messages=[{"role": "user", "content": "x"}])
    assert res.content == "ok"
    assert res.usage == Usage(input_tokens=100, output_tokens=200)
    assert res.cost_usd == pytest.approx(0.0042)


def test_complete_estimates_cost_when_response_missing_cost() -> None:
    def transport(req: dict) -> dict:
        return _fake_response(prompt_tokens=1_000_000, completion_tokens=1_000_000)

    c = OpenRouterClient(transport=transport)
    res = c.complete(model="anthropic/claude-3-haiku", messages=[{"role": "user", "content": "x"}])
    # haiku pricing in DEFAULT_PRICING is (0.25, 1.25) per 1M
    assert res.cost_usd == pytest.approx(1.5)


def test_complete_unknown_model_zero_cost_estimate() -> None:
    def transport(req: dict) -> dict:
        return _fake_response(prompt_tokens=100, completion_tokens=100)

    c = OpenRouterClient(transport=transport)
    res = c.complete(model="who/knows", messages=[{"role": "user", "content": "x"}])
    assert res.cost_usd == 0.0


def test_complete_includes_system_prompt_in_messages() -> None:
    captured: dict = {}

    def transport(req: dict) -> dict:
        captured.update(req)
        return _fake_response()

    c = OpenRouterClient(transport=transport)
    c.complete(
        model="anthropic/claude-3-haiku",
        messages=[{"role": "user", "content": "hello"}],
        system="be brief",
    )
    msgs = captured["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "be brief"}
    assert msgs[1]["role"] == "user"


def test_no_api_key_and_no_transport_raises() -> None:
    c = OpenRouterClient(api_key=None, transport=None)
    with pytest.raises(ModelError):
        c.complete(model="anthropic/claude-3-haiku", messages=[{"role": "user", "content": "x"}])


def test_malformed_response_raises() -> None:
    def transport(req: dict) -> dict:
        return {"choices": []}

    c = OpenRouterClient(transport=transport)
    with pytest.raises(ModelError):
        c.complete(model="anthropic/claude-3-haiku", messages=[{"role": "user", "content": "x"}])
