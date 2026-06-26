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
    ) -> Completion:
        body: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(messages, system=system),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        raw = self._post("/chat/completions", body)
        return self._parse(model, raw)

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

    def _parse(self, model: str, raw: dict[str, Any]) -> Completion:
        try:
            content = raw["choices"][0]["message"]["content"] or ""
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

        return Completion(model=model, content=content, usage=usage, cost_usd=cost, raw=raw)

    def _estimate_cost(self, model: str, usage: Usage) -> float:
        rates = self._pricing.get(model)
        if rates is None:
            return 0.0
        in_per_1m, out_per_1m = rates
        return (usage.input_tokens / 1_000_000) * in_per_1m + (
            usage.output_tokens / 1_000_000
        ) * out_per_1m


__all__ = ["OpenRouterClient", "Completion", "Usage", "ModelError", "DEFAULT_PRICING"]
