"""Run a single task firing — Phase 4 design.

Same code path as interactive coding: open an Action on the registry, gather
context (project memory if the runnable is project-linked), call the model
through OpenRouter, append session messages, attach a summary, and finish the
Action with cost + token counts.

Background = no human in the loop. That's the only difference from interactive
mode — and it's handled at the notification layer (no Telegram pushes for
`logged`-tier kinds) rather than here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .memory_client import HubClient
from .model import Completion, OpenRouterClient
from .notifier import Notifier
from .tasks import Task

log = logging.getLogger("baird.runner")


@dataclass
class FiringResult:
    action_id: str
    session_id: str
    completion: Completion
    runtime_s: float
    truncated: bool = False
    summary: str | None = None


def run_task_once(
    task: Task,
    *,
    hub: HubClient,
    model_client: OpenRouterClient,
    notifier: Notifier | None = None,
    host_id: str | None = None,
    project_root: Path | None = None,
) -> FiringResult:
    """Fire `task` once. Returns the FiringResult.

    Side effects:
      - Creates one Action row (host=host_id, task_id=task.id, model_name=...)
      - Creates one Session (mode='agent', task_id=task.id) and two Messages
      - Sets action.summary to the model's first ~600 chars on success
      - On exception: action.exit_code = 1, notifier posts a 'failure'
    """
    runnable = task.runnable
    started = time.monotonic()

    # Stable session per task — but for now we make a fresh one each firing.
    # The "persistent conversation thread" cross-firing state lands in 4b.
    session = hub.new_session(mode="agent", project_id=runnable.project_id, task_id=task.id)

    with hub.start_action(
        project_id=runnable.project_id,
        tool_name="model",
        command=f"task:{task.id}",
        host=host_id,
        task_id=task.id,
        model_name=runnable.model,
    ) as action:
        hub.append_message(session["id"], role="user", content=runnable.prompt)

        try:
            completion = model_client.complete(
                model=runnable.model,
                messages=[{"role": "user", "content": runnable.prompt}],
                max_tokens=runnable.max_tokens,
                temperature=runnable.temperature,
                system=runnable.system,
            )
        except Exception as e:
            log.exception("task %s firing failed during model call", task.id)
            action.set_summary(f"model call failed: {e}")
            if notifier is not None:
                notifier.notify(
                    kind="failure",
                    title=f"task {task.id} failed",
                    body=str(e),
                    project_id=runnable.project_id,
                    action_id=action.id,
                    task_id=task.id,
                )
            raise

        hub.append_message(
            session["id"], role="assistant", content=completion.content
        )

        # Soft-budget check: if max_tokens overran wall, just flag in result.
        truncated = completion.usage.output_tokens >= runnable.max_tokens

        action.record_usage(
            cost_usd=completion.cost_usd,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
        )
        summary = _summarize(completion.content)
        action.set_summary(summary)

    runtime = time.monotonic() - started

    if notifier is not None:
        notifier.notify(
            kind="result",
            title=f"task {task.id} done",
            body=summary,
            project_id=runnable.project_id,
            action_id=action.id,
            task_id=task.id,
        )

    return FiringResult(
        action_id=action.id,
        session_id=session["id"],
        completion=completion,
        runtime_s=runtime,
        truncated=truncated,
        summary=summary,
    )


def _summarize(text: str, *, max_chars: int = 600) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"
