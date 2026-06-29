"""OpenRouter client — Phase 4 model substrate.

Single backend for now: OpenRouter. Sync HTTP, no streaming (streaming lands
when the TUI needs it). Returns a `Completion` carrying content, usage
counters, and an estimated cost in USD.

Pricing: OpenRouter returns per-call cost in the response when the
`include` query parameter is set; we ask for it. If the field is missing we
fall back to a static price table keyed on the model id — easy to update.

The transport is pluggable for tests: pass a `transport=` callable accepting
the request dict and returning the response dict.
"""

from __future__ import annotations

import json as _json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx


# Approximate prices per 1M tokens (USD). Kept small + easy to override per call.
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    # (input_per_1m, output_per_1m)
    "anthropic/claude-3.5-sonnet": (3.0, 15.0),
    "anthropic/claude-3-haiku": (0.25, 1.25),
    "openai/gpt-4o": (2.5, 10.0),
    "openai/gpt-4o-mini": (0.15, 0.60),
    "google/gemini-2.5-pro": (1.25, 10.0),
}


# Prefix priority list for building a curated "popular models" view from the
# live OpenRouter catalog. Order = display order. First match wins per model.
POPULAR_PREFIXES: tuple[str, ...] = (
    "anthropic/claude-opus",
    "openrouter/",
    "anthropic/claude-sonnet",
    "anthropic/claude-haiku",
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3-haiku",
    "openai/o",
    "openai/gpt-4o",
    "openai/gpt-4",
    "google/gemini-2",
    "google/gemini-1.5",
    "meta-llama/llama-3",
    "deepseek/",
    "mistralai/",
    "x-ai/grok",
    "qwen/qwen",
)


def make_hub_proxy_transport(
    *,
    hub_url: str,
    auth_token: str | None,
    action_id: str | None = None,
    timeout: float = 90.0,
) -> "Transport":
    """Build a Transport that routes OpenRouter calls through a BAIRD hub.

    The hub holds the upstream API key, so the satellite doesn't need it.
    `action_id` (optional) lets the hub attribute cost + tokens to a specific
    open action without the caller having to do it client-side.
    """

    headers: dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if action_id:
        headers["X-Baird-Action-Id"] = action_id
    url = hub_url.rstrip("/") + "/v1/proxy/chat/completions"

    def _transport(req: dict[str, Any]):
        # We ignore `req["path"]` — the proxy URL is fixed.
        if req.get("stream"):
            def _stream():
                with httpx.stream(
                    "POST", url, headers=headers, json=req["body"], timeout=timeout
                ) as r:
                    r.raise_for_status()
                    for line in r.iter_lines():
                        yield line
            return _stream()
        r = httpx.post(url, headers=headers, json=req["body"], timeout=timeout)
        r.raise_for_status()
        return r.json()

    return _transport


def fetch_openrouter_catalog(timeout: float = 5.0) -> list[dict[str, Any]]:
    """Fetch the public OpenRouter model catalog. No auth required."""
    r = httpx.get("https://openrouter.ai/api/v1/models", timeout=timeout)
    r.raise_for_status()
    return r.json().get("data", [])


def top_openrouter_models(
    catalog: list[dict[str, Any]] | None = None, n: int = 20
) -> list[dict[str, Any]]:
    """Pick the top N popular models from the catalog (or fetched live)."""
    cat = catalog if catalog is not None else fetch_openrouter_catalog()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for prefix in POPULAR_PREFIXES:
        for m in cat:
            mid = m.get("id", "")
            if mid in seen:
                continue
            if mid.startswith(prefix):
                seen.add(mid)
                out.append(m)
                if len(out) >= n:
                    return out
    return out


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Completion:
    model: str
    content: str
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)
    # OpenAI-style tool calls returned by the model, if any. Each entry has
    # `id` (str), `name` (str), and `arguments` (dict parsed from JSON).
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


Transport = Callable[[dict[str, Any]], dict[str, Any]]


class ModelError(RuntimeError):
    pass


class OpenRouterClient:
    """OpenRouter HTTP client.

    By default reads `OPENROUTER_API_KEY` from env. Pass `transport=` to
    bypass the network entirely in tests.
    """

    BASE = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 60.0,
        transport: Transport | None = None,
        pricing: dict[str, tuple[float, float]] | None = None,
    ):
        self._key = api_key or os.getenv("OPENROUTER_API_KEY")
        self._timeout = timeout
        self._transport = transport
        self._pricing = pricing or DEFAULT_PRICING

    # --- public ----------------------------------------------------------

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 1024,
        temperature: float = 0.2,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> Completion:
        body: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(messages, system=system),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
        raw = self._post("/chat/completions", body)
        return self._parse(model, raw)

    def stream_complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 1024,
        temperature: float = 0.2,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        on_chunk: "Callable[[str], None] | None" = None,
    ) -> Completion:
        """Same as `complete`, but streams the response. Calls `on_chunk` for
        every content delta as it arrives. Returns the final Completion once
        the stream ends.

        Supports `tools` — tool_call deltas are accumulated from the stream
        and returned as structured `tool_calls` on the Completion.

        For a hub-proxy `transport=`, the transport receives the request and
        returns an iterator yielding raw SSE bytes (newline-terminated lines).
        For the default transport, this method calls OpenRouter directly with
        `stream=True` over httpx.
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(messages, system=system),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
        chunks_iter = self._stream_post("/chat/completions", body)
        # If the transport doesn't actually stream (returns a dict), parse it
        # as a non-streaming response and call the callback once with the
        # full content. Keeps tests with simple transports working.
        if isinstance(chunks_iter, dict):
            completion = self._parse(model, chunks_iter)
            if on_chunk and completion.content:
                on_chunk(completion.content)
            return completion
        content_parts: list[str] = []
        usage: dict[str, Any] | None = None
        # Accumulate tool_calls by index — each index can have id, name, args
        # spread across multiple stream deltas.
        tool_call_acc: dict[int, dict[str, Any]] = {}
        _finish_reason: str | None = None
        try:
            for line_bytes in chunks_iter:
                line = (
                    line_bytes.decode("utf-8", errors="replace")
                    if isinstance(line_bytes, bytes) else line_bytes
                )
                line = line.rstrip("\n")
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = _json.loads(payload)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]
                for ch in obj.get("choices", []) or []:
                    if ch.get("finish_reason"):
                        _finish_reason = ch["finish_reason"]
                    delta = ch.get("delta") or {}
                    # Content delta
                    d_content = delta.get("content")
                    if d_content:
                        content_parts.append(d_content)
                        if on_chunk is not None:
                            on_chunk(d_content)
                    # Tool-call deltas
                    for tc_delta in delta.get("tool_calls") or []:
                        idx = tc_delta.get("index", 0)
                        acc = tool_call_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if tc_delta.get("id"):
                            acc["id"] = tc_delta["id"]
                        if tc_delta.get("function"):
                            fn = tc_delta["function"]
                            if fn.get("name"):
                                acc["name"] = fn["name"]
                            if fn.get("arguments"):
                                acc["args"] += fn["arguments"]
        finally:
            pass
        # Build tool_calls from accumulator
        tool_calls_out: list[dict[str, Any]] | None = None
        if tool_call_acc:
            tool_calls_out = []
            for idx in sorted(tool_call_acc):
                acc = tool_call_acc[idx]
                tc = {
                    "id": acc["id"],
                    "type": "function",
                    "function": {
                        "name": acc["name"],
                        "arguments": acc["args"],
                    },
                }
                tool_calls_out.append(tc)
                # Stream tool_calls to on_chunk as JSON so the TUI can see them
                if on_chunk is not None:
                    on_chunk(_json.dumps({"tool_calls": [tc]}))
        raw_like = {
            "choices": [{"message": {"content": "".join(content_parts)}}],
            "usage": usage or {},
        }
        if tool_calls_out:
            raw_like["choices"][0]["message"]["tool_calls"] = tool_calls_out
        return self._parse(model, raw_like)

    # --- internals -------------------------------------------------------

    def _build_messages(
        self, messages: list[dict[str, Any]], *, system: str | None
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        out.extend(messages)
        return out

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if self._transport is not None:
            return self._transport({"path": path, "body": body})
        if not self._key:
            raise ModelError("OPENROUTER_API_KEY not set and no transport supplied")
        with httpx.Client(timeout=self._timeout) as client:
            r = client.post(
                f"{self.BASE}{path}",
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/ethanlewisbaird/BAIRD",
                    "X-Title": "BAIRD",
                },
                json=body,
            )
            if r.status_code >= 400:
                raise ModelError(f"openrouter {r.status_code}: {r.text[:500]}")
            return r.json()

    def _stream_post(self, path: str, body: dict[str, Any]):
        """Yield raw SSE lines. With a `transport`, the transport returns an
        iterator. Without one, hits OpenRouter directly with `stream=True`."""
        if self._transport is not None:
            return self._transport({"path": path, "body": body, "stream": True})
        if not self._key:
            raise ModelError("OPENROUTER_API_KEY not set and no transport supplied")

        def _gen():
            with httpx.stream(
                "POST",
                f"{self.BASE}{path}",
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    "HTTP-Referer": "https://github.com/ethanlewisbaird/BAIRD",
                    "X-Title": "BAIRD",
                },
                json=body,
                timeout=self._timeout,
            ) as r:
                if r.status_code >= 400:
                    raise ModelError(f"openrouter {r.status_code}")
                for line in r.iter_lines():
                    yield line

        return _gen()

    def _parse(self, model: str, raw: dict[str, Any]) -> Completion:
        try:
            msg = raw["choices"][0]["message"]
            content = msg.get("content") or ""
        except (KeyError, IndexError) as e:
            raise ModelError(f"unexpected response shape: {raw}") from e
        usage_raw = raw.get("usage") or {}
        usage = Usage(
            input_tokens=usage_raw.get("prompt_tokens", 0),
            output_tokens=usage_raw.get("completion_tokens", 0),
        )

        cost = float(usage_raw.get("cost", 0.0) or 0.0)
        if cost == 0.0:
            cost = self._estimate_cost(model, usage)

        # OpenAI-style tool_calls (passed through OpenRouter for models that
        # support function calling). Each entry: {id, type, function: {name,
        # arguments(JSON string)}}. We flatten + parse arguments here so the
        # REPL doesn't have to know the wire shape.
        tool_calls: list[dict[str, Any]] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments")
            args: dict[str, Any]
            if isinstance(raw_args, str):
                try:
                    args = _json.loads(raw_args) if raw_args.strip() else {}
                except _json.JSONDecodeError:
                    args = {"_raw": raw_args}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            tool_calls.append({
                "id": tc.get("id") or "",
                "name": fn.get("name") or "",
                "arguments": args,
            })

        return Completion(
            model=model, content=content, usage=usage, cost_usd=cost,
            raw=raw, tool_calls=tool_calls,
        )

    def _estimate_cost(self, model: str, usage: Usage) -> float:
        rates = self._pricing.get(model)
        if rates is None:
            return 0.0
        in_per_1m, out_per_1m = rates
        return (usage.input_tokens / 1_000_000) * in_per_1m + (
            usage.output_tokens / 1_000_000
        ) * out_per_1m


__all__ = ["OpenRouterClient", "Completion", "Usage", "ModelError", "DEFAULT_PRICING"]
