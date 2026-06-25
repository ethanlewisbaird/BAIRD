"""Research loop — Phase 4 design (#6).

Two flavours, same code path:
  - user-initiated burst (`baird research "<query>"`, default max_cost_usd=0.50)
  - standing watch (cron + project-linked, default max_cost_usd=0.10)

Loop shape per firing:

  plan       → ask the model for 3-5 sub-questions / search terms
  gather     → call `web_search(query)` for each sub-question
  synthesize → feed snippets back to the model for a markdown summary
  notify     → results-ready notification carrying the summary

The `web_search` callable is pluggable. The default integration is the Tavily
search API (HTTP); tests inject a fake. bioRxiv/PubMed MCPs already exist on
the user's setup — wiring them as additional searchers is a follow-up.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .memory_client import HubClient
from .model import OpenRouterClient
from .notifier import Notifier

log = logging.getLogger("baird.research")


# Search backend protocol: (query, max_results) → list[{title, url, snippet}]
WebSearchFn = Callable[[str, int], list[dict[str, str]]]


# ---- Default search backend (Tavily) -----------------------------------


def tavily_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Default web search via Tavily. Requires TAVILY_API_KEY in env.

    Returns a list of {title, url, snippet} dicts. Empty list on error so
    the research loop degrades gracefully rather than crashing.
    """
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        log.warning("TAVILY_API_KEY not set — web_search returning empty results")
        return []
    try:
        r = httpx.post(
            "https://api.tavily.com/search",
            json={"api_key": key, "query": query, "max_results": max_results, "search_depth": "basic"},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
        out: list[dict[str, str]] = []
        for hit in (data.get("results") or [])[:max_results]:
            out.append({
                "title": hit.get("title", ""),
                "url": hit.get("url", ""),
                "snippet": (hit.get("content") or "")[:500],
            })
        return out
    except Exception:
        log.exception("tavily_search failed")
        return []


# ---- Loop --------------------------------------------------------------


@dataclass
class ResearchResult:
    query: str
    action_id: str
    sub_questions: list[str] = field(default_factory=list)
    hits: list[dict[str, str]] = field(default_factory=list)
    synthesis: str = ""
    cost_usd: float = 0.0


PLAN_SYSTEM = """\
You are BAIRD's research planner. Decompose the user's query into 3-5 specific
search-engine queries that together cover the question. Return strictly JSON:

{"sub_queries": ["...", "...", ...]}
"""


SYNTH_SYSTEM = """\
You are BAIRD's research synthesizer. Given the user's original query and a
list of web search snippets, write a concise markdown brief (under 500 words):

  ## Bottom line
  <one paragraph>

  ## Key findings
  - bullets, each with a [source-N] citation

  ## Sources
  [source-N] title — url

Be conservative. If the snippets don't answer the question, say so.
"""


def run_research(
    *,
    query: str,
    hub: HubClient,
    model_client: OpenRouterClient,
    notifier: Notifier | None = None,
    web_search: WebSearchFn = tavily_search,
    project_id: str | None = None,
    host_id: str | None = None,
    model: str = "anthropic/claude-3-haiku",
    per_query_results: int = 5,
    max_subqueries: int = 5,
) -> ResearchResult:
    """Fire one research cycle. Always writes an inbox row at the end."""
    with hub.start_action(
        project_id=project_id,
        tool_name="research",
        command=f"research:{query}",
        host=host_id,
        model_name=model,
    ) as action:
        # Plan.
        plan_resp = model_client.complete(
            model=model,
            messages=[{"role": "user", "content": query}],
            system=PLAN_SYSTEM,
            max_tokens=512,
        )
        action.record_usage(
            cost_usd=plan_resp.cost_usd,
            input_tokens=plan_resp.usage.input_tokens,
            output_tokens=plan_resp.usage.output_tokens,
        )
        sub_queries = _parse_subqueries(plan_resp.content)[:max_subqueries]
        if not sub_queries:
            sub_queries = [query]

        # Gather.
        all_hits: list[dict[str, str]] = []
        for sq in sub_queries:
            hits = web_search(sq, per_query_results)
            for h in hits:
                h["_for"] = sq
            all_hits.extend(hits)

        # Synthesize.
        if all_hits:
            corpus = _render_corpus(query, sub_queries, all_hits)
            synth_resp = model_client.complete(
                model=model,
                messages=[{"role": "user", "content": corpus}],
                system=SYNTH_SYSTEM,
                max_tokens=1024,
            )
            action.record_usage(
                cost_usd=synth_resp.cost_usd,
                input_tokens=synth_resp.usage.input_tokens,
                output_tokens=synth_resp.usage.output_tokens,
            )
            synthesis = synth_resp.content
        else:
            synth_resp = None
            synthesis = (
                "No web search results returned. Set TAVILY_API_KEY or wire a "
                "different search backend, then re-run."
            )

        total_cost = plan_resp.cost_usd + (synth_resp.cost_usd if synth_resp else 0.0)
        action.set_summary(
            f"research: {len(sub_queries)} sub-q, {len(all_hits)} hits, ${total_cost:.4f}"
        )

    if notifier is not None:
        notifier.notify(
            kind="result",
            title=f"research: {query[:60]}",
            body=synthesis,
            project_id=project_id,
            action_id=action.id,
        )

    return ResearchResult(
        query=query,
        action_id=action.id,
        sub_questions=sub_queries,
        hits=all_hits,
        synthesis=synthesis,
        cost_usd=total_cost,
    )


# ---- Helpers -----------------------------------------------------------


def _parse_subqueries(content: str) -> list[str]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out = data.get("sub_queries") or []
    return [s for s in out if isinstance(s, str) and s.strip()]


def _render_corpus(query: str, sub_queries: list[str], hits: list[dict[str, str]]) -> str:
    parts = [f"# Original query\n{query}\n", "# Sub-queries", *(f"- {q}" for q in sub_queries), "", "# Snippets"]
    for i, h in enumerate(hits, 1):
        parts.append(
            f"[source-{i}] ({h.get('_for', '')})\n"
            f"  title: {h.get('title', '')}\n"
            f"  url: {h.get('url', '')}\n"
            f"  snippet: {h.get('snippet', '')}\n"
        )
    return "\n".join(parts)
