"""Rolling-summary compressor for long conversation histories.

The runner and REPL load the last N messages from a session each turn (default
N=20). Past N, history was being dropped silently. This module replaces that
hard cut with a synthesised summary of the older turns, prepended as a system
message so the model still sees the gist.

In-process cache: keyed by `(session_id, total_message_count)` so a session
that hasn't grown since the last call doesn't trigger a redundant summary
roundtrip.
"""

from __future__ import annotations

from typing import Any

from .memory_client import HubClient
from .model import OpenRouterClient


_SUMMARY_SYSTEM = (
    "You compress prior conversation turns into a single dense paragraph that "
    "preserves: the user's goals, decisions made, code/file references, and "
    "any blockers. Aim for under 250 words. Return only the summary."
)


# (session_id, msg_count_at_summarisation) → summary string
_cache: dict[tuple[str, int], str] = {}


def _summarise_messages(
    model_client: OpenRouterClient,
    messages: list[dict[str, Any]],
    *,
    model: str,
) -> str:
    """Call the model to compress a list of messages into one paragraph."""
    body = "\n\n".join(f"[{m['role']}] {m['content']}" for m in messages)
    completion = model_client.complete(
        model=model,
        messages=[{"role": "user", "content": body}],
        system=_SUMMARY_SYSTEM,
        max_tokens=400,
        temperature=0.1,
    )
    return completion.content.strip()


def load_history_with_summary(
    hub: HubClient,
    *,
    session_id: str,
    cap: int,
    model_client: OpenRouterClient,
    summary_model: str = "openrouter/owl-alpha",
) -> list[dict[str, Any]]:
    """Return up to `cap` recent messages, prepending a synthetic system
    message summarising anything older if the session has more than `cap`
    entries. Returns an empty list when the session has no history."""
    # Walk all pages — hub caps `limit` at 1000 per call but supports offset.
    all_msgs: list[dict[str, Any]] = []
    page = 1000
    while True:
        chunk = hub.get_messages(session_id, limit=page, offset=len(all_msgs))
        if not chunk:
            break
        all_msgs.extend(chunk)
        if len(chunk) < page:
            break
    if len(all_msgs) <= cap:
        return [_sanitised(m) for m in all_msgs]

    older = all_msgs[: len(all_msgs) - cap]
    recent = all_msgs[len(all_msgs) - cap :]

    cache_key = (session_id, len(older))
    summary = _cache.get(cache_key)
    if summary is None:
        try:
            summary = _summarise_messages(
                model_client,
                [_sanitised(m) for m in older],
                model=summary_model,
            )
        except Exception:
            # Compressor failure must not block the turn — drop older silently.
            summary = None
        if summary:
            _cache[cache_key] = summary

    head: list[dict[str, Any]] = []
    if summary:
        head.append({
            "role": "system",
            "content": f"[previous conversation summary]\n{summary}",
        })
    head.extend(_sanitised(m) for m in recent)
    return head


def _sanitised(msg: dict[str, Any]) -> dict[str, Any]:
    """Project a stored Message down to the shape the model expects, and
    strip any text-shaped tool-call markup from assistant content so the
    model doesn't see (and mimic) its own past failed attempts."""
    from .repl import contains_text_tool_call, strip_text_tool_calls

    content = msg.get("content") or ""
    if msg.get("role") == "assistant" and content and contains_text_tool_call(content):
        stripped = strip_text_tool_calls(content)
        # If the stripped content is empty, keep a marker so the conversation
        # turn shape stays valid (some providers reject empty-content messages)
        # without re-exposing the original markup.
        content = stripped or "(text-shaped tool call removed from history)"
    return {"role": msg["role"], "content": content}


def clear_cache() -> None:
    """Test helper / hot-reload escape hatch."""
    _cache.clear()
