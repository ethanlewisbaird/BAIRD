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
from .tasks import Task, resolve_project_ids

log = logging.getLogger("baird.runner")


@dataclass
class FiringResult:
    action_id: str
    session_id: str
    completion: Completion | None
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

    Dispatches on `runnable.kind`:
      - `model` (default): free-form completion against runnable.prompt.
      - `self_improve`: runs `baird.self_improve.run_self_improvement`.
      - `research`: runs `baird.research.run_research` with runnable.args.

    Side effects:
      - Creates one Action row (host=host_id, task_id=task.id, model_name=...)
      - Creates one Session (mode='agent', task_id=task.id) and two Messages
      - Sets action.summary to the model's first ~600 chars on success
      - On exception: action.exit_code = 1, notifier posts a 'failure'
    """
    runnable = task.runnable
    started = time.monotonic()

    # Dispatch on kind. `self_improve` / `research` / `command` bypass the
    # model-prompt path entirely — they own their action accounting.
    if runnable.kind == "self_improve":
        return _run_self_improve(task, hub=hub, model_client=model_client, notifier=notifier)
    if runnable.kind == "research":
        return _run_research(task, hub=hub, model_client=model_client, notifier=notifier)
    if runnable.kind == "command":
        from .dispatcher import run_command_task

        result = run_command_task(
            task,
            hub=hub,
            hub_host_id=host_id,
            project_root=project_root,
            notifier=notifier,
        )
        return FiringResult(
            action_id=result["action_id"],
            session_id="",
            completion=None,
            runtime_s=result["runtime_s"],
            truncated=False,
            summary=f"exit={result['exit_code']}",
        )

    # Persistent per-task conversation thread (Phase 4b): one Session per
    # task_id, reused across firings. Context compressor / rolling summary
    # is its own slice — for now we cap history at the last 20 messages.
    session = hub.find_or_create_session_for_task(
        task_id=task.id, project_id=runnable.project_id, mode="agent"
    )

    with hub.start_action(
        project_id=runnable.project_id,
        tool_name="model",
        command=f"task:{task.id}",
        host=host_id,
        task_id=task.id,
        model_name=runnable.model,
    ) as action:
        from .context_compressor import load_history_with_summary

        prior_msgs = load_history_with_summary(
            hub,
            session_id=session["id"],
            cap=20,
            model_client=model_client,
            summary_model=runnable.model,
        )
        hub.append_message(session["id"], role="user", content=runnable.prompt)
        prior_msgs.append({"role": "user", "content": runnable.prompt})

        try:
            completion = model_client.complete(
                model=runnable.model,
                messages=prior_msgs,
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


def run_task_fanout(
    task: Task,
    *,
    hub: HubClient,
    model_client: OpenRouterClient,
    notifier: Notifier | None = None,
    host_id: str | None = None,
    project_root: Path | None = None,
) -> list[FiringResult]:
    """Resolve `runnable.project_ids` (expanding parents to children) and fire
    once per resolved id. Returns one FiringResult per firing.

    If neither `project_ids` nor a parent expansion applies, this is identical
    to a single `run_task_once` and returns a list of length one."""
    runnable = task.runnable
    resolved = resolve_project_ids(runnable, hub)
    # No fanout needed — preserve singular-task semantics.
    if len(resolved) == 1 and resolved[0] == runnable.project_id:
        return [run_task_once(
            task,
            hub=hub,
            model_client=model_client,
            notifier=notifier,
            host_id=host_id,
            project_root=project_root,
        )]

    results: list[FiringResult] = []
    for pid in resolved:
        # Shallow-clone the task so each firing carries its own project_id
        # without mutating the caller's task.
        per_runnable = runnable.model_copy(update={"project_id": pid, "project_ids": []})
        per_task = task.model_copy(update={"runnable": per_runnable})
        try:
            results.append(
                run_task_once(
                    per_task,
                    hub=hub,
                    model_client=model_client,
                    notifier=notifier,
                    host_id=host_id,
                    project_root=project_root,
                )
            )
        except Exception:
            log.exception(
                "task %s firing for project=%s raised; continuing fanout",
                task.id,
                pid,
            )
    return results


def _summarize(text: str, *, max_chars: int = 600) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


# ----- kind dispatch ---------------------------------------------------


def _run_self_improve(
    task: Task,
    *,
    hub: HubClient,
    model_client: OpenRouterClient,
    notifier: Notifier | None,
) -> FiringResult:
    from .self_improve import run_self_improvement

    runnable = task.runnable
    started = time.monotonic()
    since_hours = int(runnable.args.get("since_hours", 168))
    proposals = run_self_improvement(
        hub=hub,
        model_client=model_client,
        notifier=notifier,
        since_hours=since_hours,
        model=runnable.model,
    )
    runtime = time.monotonic() - started
    summary = f"{len(proposals)} proposals"
    return FiringResult(
        action_id="",  # self_improve writes its own action
        session_id="",
        completion=None,
        runtime_s=runtime,
        truncated=False,
        summary=summary,
    )


def _run_research(
    task: Task,
    *,
    hub: HubClient,
    model_client: OpenRouterClient,
    notifier: Notifier | None,
) -> FiringResult:
    from .research import run_research

    runnable = task.runnable
    started = time.monotonic()
    query = runnable.args.get("query") or runnable.prompt
    if not query:
        raise ValueError("research task needs runnable.args.query (or prompt)")
    brief = run_research(
        query=query,
        hub=hub,
        model_client=model_client,
        notifier=notifier,
        project_id=runnable.project_id,
        model=runnable.model,
    )
    runtime = time.monotonic() - started
    return FiringResult(
        action_id="",
        session_id="",
        completion=None,
        runtime_s=runtime,
        truncated=False,
        summary=_summarize(brief or ""),
    )
