"""Model-proxy routes on the hub.

Centralises OpenRouter access: satellites POST to the hub, the hub forwards
to OpenRouter using its own key. The hub records cost + tokens against the
caller's action if `X-Baird-Action-Id` is sent.

Design intent: one place for the key, one ledger of spend, one place to swap
providers later. The route shape mirrors OpenRouter's so a satellite client
can swap base URLs without re-implementing parsing.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator, Iterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .db import Action


log = logging.getLogger(__name__)


# The function the proxy uses to call OpenRouter (non-streaming). Overridable for tests.
def _default_forward(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    r = httpx.post(url, headers=headers, json=body, timeout=90.0)
    r.raise_for_status()
    return r.json()


# Streaming variant. Yields raw upstream SSE line bytes; the route writes them
# back to the satellite verbatim. Overridable for tests.
def _default_stream_forward(
    url: str, headers: dict[str, str], body: dict[str, Any]
) -> Iterator[bytes]:
    with httpx.stream(
        "POST", url, headers=headers, json=body, timeout=300.0
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if line:
                yield (line + "\n").encode("utf-8")
            else:
                yield b"\n"


# Tests overwrite these attributes on the module.
forward_call = _default_forward
stream_forward_call = _default_stream_forward


def register_routes(app: FastAPI) -> None:
    @app.post("/v1/proxy/chat/completions")
    async def proxy_chat_completions(request: Request):
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
        url = f"{cfg.openrouter_url.rstrip('/')}/chat/completions"

        if not body.get("stream"):
            try:
                raw = forward_call(url, upstream_headers, body)
            except httpx.HTTPError as e:
                raise HTTPException(502, f"upstream error: {e}") from e

            if action_id:
                usage = raw.get("usage", {}) or {}
                _record_usage(
                    request.app, action_id,
                    input_t=usage.get("prompt_tokens") or 0,
                    output_t=usage.get("completion_tokens") or 0,
                    cost=usage.get("cost"),
                )
            return raw

        # Streaming path: forward SSE to the caller, accumulate usage from
        # the last chunk that carries it, enrich the action when done.
        def _gen():
            usage: dict[str, Any] | None = None
            try:
                for chunk in stream_forward_call(url, upstream_headers, body):
                    text = chunk.decode("utf-8", errors="replace").rstrip("\n")
                    if text.startswith("data: ") and text != "data: [DONE]":
                        try:
                            obj = json.loads(text[6:])
                            if isinstance(obj, dict) and obj.get("usage"):
                                usage = obj["usage"]
                        except json.JSONDecodeError:
                            pass
                    yield chunk
            except Exception as e:
                log.warning("proxy stream upstream error: %s", e)
                err = json.dumps({"error": str(e)})
                yield (f"data: {err}\n\n").encode("utf-8")
            finally:
                if action_id and usage:
                    _record_usage(
                        request.app, action_id,
                        input_t=usage.get("prompt_tokens") or 0,
                        output_t=usage.get("completion_tokens") or 0,
                        cost=usage.get("cost"),
                    )

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


def _record_usage(app, action_id: str, *, input_t: int, output_t: int, cost: float | None) -> None:
    factory = app.state.registry_session
    with factory() as s:
        _enrich_action(s, action_id, input_t, output_t, cost)
        s.commit()


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
