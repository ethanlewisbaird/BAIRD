"""Multi-turn coding REPL for `baird code` — Phase 4b.

Each user line is a turn:
  1. Append user message to the persistent project Session (one per project).
  2. Open a per-turn Action (so budgets + history attribute cost correctly).
  3. Call the model with the recent message history + system prompt.
  4. Append assistant message; record cost on the action.
  5. Print the reply + per-turn cost/token footer.

Special inputs start with `/`:
  /exit, /quit        — leave the REPL
  /context            — re-render the repo context block
  /reset              — start a fresh session (drops prior history)
  /cost               — show cumulative cost for this REPL invocation

Diff approval / tool calling lives in a later slice — we deliberately keep
this loop's contract small so the substrate is solid first.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel

from .context_loader import RepoContext, render_context
from .memory_client import HubClient
from .model import Completion, ModelError, OpenRouterClient


HISTORY_TURN_CAP = 20  # last N messages sent to the model on each turn


@dataclass
class ReplStats:
    turns: int = 0
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


@dataclass
class ReplConfig:
    project_id: str
    model: str = "anthropic/claude-3-haiku"
    max_tokens: int = 1024
    temperature: float = 0.2
    history_cap: int = HISTORY_TURN_CAP


def _system_prompt(rendered_context: str) -> str:
    return (
        "You are BAIRD, a bioinformatics research assistant. The active project's "
        "context follows. Be concise; when proposing code, give the change as a "
        "fenced unified diff so it can be reviewed and applied.\n\n"
        + rendered_context
    )


def run_repl(
    *,
    repo_ctx: RepoContext,
    hub: HubClient,
    model_client: OpenRouterClient,
    config: ReplConfig,
    console: Console,
    input_fn: Callable[[str], str] = input,
    inputs: Iterable[str] | None = None,
    host_id: str | None = None,
) -> ReplStats:
    """Run the REPL until `/exit`, EOF, or the `inputs` iterable is exhausted.

    `inputs` is for tests — when supplied, lines are consumed from it instead
    of calling `input_fn`. EOF behaves like `/exit`.
    """
    stats = ReplStats()
    rendered = render_context(repo_ctx)
    system = _system_prompt(rendered)

    session = hub.find_or_create_session_for_task(
        task_id=f"repl-{config.project_id}",
        project_id=config.project_id,
        mode="code",
    )
    console.print(
        Panel.fit(
            f"[green]baird code[/green]  project={config.project_id}  model={config.model}\n"
            f"session={session['id'][:8]}  type /exit to quit, /context to dump context",
            border_style="green",
        )
    )

    iterator: Optional[Iterable[str]] = iter(inputs) if inputs is not None else None

    while True:
        try:
            if iterator is not None:
                try:
                    raw = next(iterator)
                except StopIteration:
                    break
            else:
                raw = input_fn("user> ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            break

        line = raw.strip()
        if not line:
            continue

        if line.startswith("/"):
            cmd = line[1:].split()[0].lower()
            if cmd in {"exit", "quit"}:
                break
            if cmd == "context":
                console.print(rendered)
                continue
            if cmd == "reset":
                session = hub.new_session(
                    mode="code",
                    project_id=config.project_id,
                    task_id=f"repl-{config.project_id}",
                )
                console.print(f"[yellow]new session[/yellow] {session['id'][:8]}")
                continue
            if cmd == "cost":
                console.print(
                    f"[dim]turns={stats.turns}  cost=${stats.total_cost_usd:.4f}  "
                    f"tokens={stats.total_input_tokens}→{stats.total_output_tokens}[/dim]"
                )
                continue
            console.print(f"[red]unknown command:[/red] /{cmd}")
            continue

        try:
            completion = _one_turn(
                user_msg=line,
                hub=hub,
                model_client=model_client,
                session_id=session["id"],
                config=config,
                system=system,
                host_id=host_id,
            )
        except ModelError as e:
            console.print(f"[red]model error:[/red] {e}")
            continue

        console.print(completion.content)
        console.print(
            f"[dim]model={completion.model}  "
            f"tokens={completion.usage.input_tokens}→{completion.usage.output_tokens}  "
            f"cost=${completion.cost_usd:.4f}[/dim]"
        )
        stats.turns += 1
        stats.total_cost_usd += completion.cost_usd
        stats.total_input_tokens += completion.usage.input_tokens
        stats.total_output_tokens += completion.usage.output_tokens

    console.print(
        f"[dim]session={session['id'][:8]}  turns={stats.turns}  total=${stats.total_cost_usd:.4f}[/dim]"
    )
    return stats


def _one_turn(
    *,
    user_msg: str,
    hub: HubClient,
    model_client: OpenRouterClient,
    session_id: str,
    config: ReplConfig,
    system: str,
    host_id: str | None,
) -> Completion:
    """Append the user message, call the model with recent history, record an
    Action with cost, append the assistant message. Returns the Completion."""
    with hub.start_action(
        project_id=config.project_id,
        tool_name="model",
        command="repl",
        host=host_id,
        model_name=config.model,
    ) as action:
        prior = hub.get_messages(session_id, limit=config.history_cap)
        msgs = [{"role": m["role"], "content": m["content"]} for m in prior]
        hub.append_message(session_id, role="user", content=user_msg)
        msgs.append({"role": "user", "content": user_msg})

        completion = model_client.complete(
            model=config.model,
            messages=msgs,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            system=system,
        )

        hub.append_message(session_id, role="assistant", content=completion.content)
        action.record_usage(
            cost_usd=completion.cost_usd,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
        )
        # Summary for the action row — first line of the reply (or truncated).
        first_line = completion.content.strip().splitlines()[0] if completion.content else ""
        action.set_summary(first_line[:200])

    return completion
