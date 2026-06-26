"""Model-proxy routes on the hub.

Centralises OpenRouter access: satellites POST to the hub, the hub forwards
to OpenRouter using its own key. The hub records cost + tokens against the
caller's action if `X-Baird-Action-Id` is sent.

Design intent: one place for the key, one ledger of spend, one place to swap
providers later. The route shape mirrors OpenRouter's so a satellite client
can swap base URLs without re-implementing parsing.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import update
from sqlalchemy.orm import Session

from .db import Action
from .hub import get_registry


# The function the proxy uses to call OpenRouter. Overridable for tests.
def _default_forward(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    r = httpx.post(url, headers=headers, json=body, timeout=90.0)
    r.raise_for_status()
    return r.json()


# Tests overwrite this attribute on the module.
forward_call = _default_forward


def register_routes(app: FastAPI) -> None:
    @app.post("/v1/proxy/chat/completions")
    async def proxy_chat_completions(request: Request) -> dict[str, Any]:
        cfg = request.app.state.hub_cfg
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise HTTPException(500, "hub has no OPENROUTER_API_KEY in env")

        body = await request.json()
        action_id = request.headers.get("x-baird-action-id")

        upstream_headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            raw = forward_call(
                f"{cfg.openrouter_url.rstrip('/')}/chat/completions",
                upstream_headers,
                body,
            )
        except httpx.HTTPError as e:
            raise HTTPException(502, f"upstream error: {e}") from e

        # Record cost / tokens against the calling action, if it's still open.
        if action_id:
            usage = raw.get("usage", {}) or {}
            input_t = usage.get("prompt_tokens") or 0
            output_t = usage.get("completion_tokens") or 0
            cost = usage.get("cost")
            factory = request.app.state.registry_session
            with factory() as s:
                _enrich_action(s, action_id, input_t, output_t, cost)
                s.commit()

        return raw


def _enrich_action(
    s: Session, action_id: str, input_t: int, output_t: int, cost: float | None
) -> None:
    """Add the call's tokens/cost to the action row. Best-effort; we silently
    skip if the action doesn't exist (the caller will see the model response
    either way)."""
    row = s.get(Action, action_id)
    if row is None:
        return
    row.input_tokens = (row.input_tokens or 0) + input_t
    row.output_tokens = (row.output_tokens or 0) + output_t
    if cost is not None:
        row.cost_usd = (row.cost_usd or 0.0) + cost
