"""Self-improvement loop — Phase 4 design (#5).

Runs on a weekly cron by default; burst-on-demand via `baird improve`. Reads
the recent action history (default last 24h, configurable up to 7d), summarises
failures / rule violations / longer-than-expected runs / user-flagged
unsatisfactory outputs / cross-firing patterns, and asks the model to propose
three kinds of changes (all from day one — broad scope per the design):

  (a) prompt edits        — diff against the harness config repo
  (b) new rules           — proposed additions to the standard rule set
  (c) task tuning         — patches to task YAMLs

Proposals are NEVER auto-applied. They land as inbox `proposal` rows carrying
the rationale + diff text + links to evidence (action IDs). User reviews them
with `baird inbox`; accepted proposals go through the normal diff → approval
cycle (Phase 3).

Budget: per design default `max_cost_usd: 0.30` per firing.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .memory_client import HubClient
from .model import OpenRouterClient
from .notifier import Notifier
from .tasks import Task, load_tasks_dir

log = logging.getLogger("baird.self_improve")


SYSTEM_PROMPT = """\
You are BAIRD's self-improvement reviewer. Examine the recent harness activity
below and propose up to FIVE concrete changes. Return strictly JSON of the form:

{"proposals": [
  {
    "kind": "prompt" | "rule" | "task",
    "title": "<short title>",
    "rationale": "<2-3 sentences citing the evidence>",
    "evidence_action_ids": ["..."],
    "diff_or_text": "<unified diff for prompt/task kinds; YAML rule body for rule kind>"
  }
]}

Be specific. Cite action IDs in evidence_action_ids. Prefer one strong proposal
over five weak ones. If nothing is worth changing, return {"proposals": []}.
"""


@dataclass
class ImprovementResult:
    action_id: str
    proposals: list[dict]
    notification_ids: list[str]
    cost_usd: float


def run_self_improvement(
    *,
    hub: HubClient,
    model_client: OpenRouterClient,
    notifier: Notifier | None = None,
    since_hours: int = 24,
    model: str = "anthropic/claude-3.5-sonnet",
    host_id: str | None = None,
    tasks_dir: Path | None = None,
    max_actions: int = 200,
) -> ImprovementResult:
    """Fire one improvement cycle."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=since_hours)
    actions = hub.list_actions(started_after=cutoff, limit=max_actions)
    tasks = load_tasks_dir(tasks_dir) if tasks_dir else {}
    rendered_corpus = _render_corpus(actions, tasks)

    with hub.start_action(
        tool_name="self_improve",
        command="self_improve",
        host=host_id,
        model_name=model,
    ) as action:
        completion = model_client.complete(
            model=model,
            messages=[{"role": "user", "content": rendered_corpus}],
            system=SYSTEM_PROMPT,
            max_tokens=2048,
        )
        action.record_usage(
            cost_usd=completion.cost_usd,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
        )

        proposals = _parse_proposals(completion.content)
        action.set_summary(
            f"{len(proposals)} proposal(s); cost=${completion.cost_usd:.4f}"
        )

    notification_ids: list[str] = []
    if notifier is not None:
        for p in proposals:
            evidence = ", ".join(p.get("evidence_action_ids", [])[:5])
            body = (
                f"Rationale: {p.get('rationale', '')}\n\n"
                f"Evidence actions: {evidence or '(none cited)'}\n\n"
                f"--- proposed change ---\n{p.get('diff_or_text', '')}"
            )
            row = notifier.notify(
                kind="proposal",
                title=f"[{p.get('kind', 'unknown')}] {p.get('title', 'untitled')}",
                body=body,
                action_id=action.id,
            )
            notification_ids.append(row["id"])

    return ImprovementResult(
        action_id=action.id,
        proposals=proposals,
        notification_ids=notification_ids,
        cost_usd=completion.cost_usd,
    )


def _parse_proposals(content: str) -> list[dict]:
    """Pull the JSON object out of the model's reply.

    Models sometimes wrap JSON in ```json fences — strip those before parsing.
    """
    text = content.strip()
    if text.startswith("```"):
        # remove first fence line + last fence line
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
    proposals = data.get("proposals", [])
    return [p for p in proposals if isinstance(p, dict)]


def _render_corpus(actions: list[dict], tasks: dict[str, Task]) -> str:
    parts: list[str] = []

    parts.append(f"# Recent action history ({len(actions)} actions)\n")
    for a in actions:
        parts.append(
            f"- id={a['id'][:8]} project={a.get('project_id') or '-'} "
            f"task={a.get('task_id') or '-'} "
            f"exit={a.get('exit_code') if a.get('exit_code') is not None else '…'} "
            f"cost=${a.get('cost_usd') or 0:.4f} "
            f"cmd={(a.get('command') or '?')[:80]}\n"
            f"  summary: {(a.get('summary') or '(none)')[:200]}"
        )

    if tasks:
        parts.append("\n# Declared tasks\n")
        for tid, t in tasks.items():
            parts.append(
                f"- id={tid} trigger={t.trigger.type} enabled={t.enabled} "
                f"model={t.runnable.model} prompt={(t.runnable.prompt or '')[:80]!r}"
            )

    return "\n".join(parts)
