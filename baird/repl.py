"""Multi-turn coding REPL for `baird code` — Phase 4b + diff loop.

Each user line is a turn:
  1. Append user message to the persistent project Session (one per project).
  2. Open a per-turn Action (so budgets + history attribute cost correctly).
  3. Call the model with the recent message history + system prompt.
  4. Append assistant message; record cost on the action.
  5. Print the reply + per-turn cost/token footer.
  6. If the reply contains fenced ```diff blocks, prompt for per-block approval.

Special inputs start with `/`:
  /exit, /quit        — leave the REPL
  /context            — re-render the repo context block
  /reset              — start a fresh session (drops prior history)
  /cost               — show cumulative cost for this REPL invocation
  /model [id]         — show or change the OpenRouter model mid-session
  /no-diff            — skip diff prompting for the rest of the session
  /help               — list slash commands
"""

from __future__ import annotations

import json as _json
import re
import sys
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from .context_loader import RepoContext
from .diff_apply import DiffApplyError, apply_diff_to_repo
from .memory_client import HubClient
from .model import Completion, ModelError, OpenRouterClient


# ---- Git snapshot helpers -----------------------------------------------


@dataclass
class GitSnapshot:
    """Pre- or post-turn git state."""

    diff: str  # git diff (unstaged)
    cached: str  # git diff --cached (staged)
    status: str  # git status --porcelain
    files: list[str]  # files changed in this turn


def _capture_git_snapshot(project_root: Path) -> GitSnapshot | None:
    """Capture git state at a point in time. Returns None when the directory
    is not a git repo or git is unavailable."""
    import subprocess as _sp

    try:
        diff = _sp.run(
            ["git", "diff"], cwd=str(project_root),
            capture_output=True, text=True, timeout=5,
        ).stdout or ""
        cached = _sp.run(
            ["git", "diff", "--cached"], cwd=str(project_root),
            capture_output=True, text=True, timeout=5,
        ).stdout or ""
        status = _sp.run(
            ["git", "status", "--porcelain"], cwd=str(project_root),
            capture_output=True, text=True, timeout=5,
        ).stdout or ""
        # Parse changed files from status
        files = []
        for line in status.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                files.append(parts[1])
        return GitSnapshot(diff=diff, cached=cached, status=status, files=files)
    except Exception:
        return None


def _diff_snapshots(
    before: GitSnapshot | None, after: GitSnapshot | None,
) -> str | None:
    """Produce a human-readable summary of what changed between two snapshots.
    Returns None when nothing changed or snapshots aren't available."""
    if before is None or after is None:
        return None
    new_files = set(after.files) - set(before.files)
    removed_files = set(before.files) - set(after.files)
    changed_files = set(after.files) & set(before.files)
    changed = [f for f in changed_files if before.status.find(f) != after.status.find(f)]

    parts: list[str] = []
    if new_files:
        parts.append(f"new: {', '.join(sorted(new_files))}")
    if removed_files:
        parts.append(f"removed: {', '.join(sorted(removed_files))}")
    if after.diff:
        parts.append(f"diff:\n{after.diff[:2000]}")

    if not parts:
        return None
    return "\n".join(parts)


_DIFF_BLOCK_RE = re.compile(r"```(?:diff|patch)\s*\n(.*?)```", re.DOTALL)


def extract_diff_blocks(text: str) -> list[str]:
    """Pull any ```diff / ```patch fenced blocks out of `text`."""
    return [m.group(1) for m in _DIFF_BLOCK_RE.finditer(text or "")]


# Patterns we use to detect when a model has emitted a text-shaped tool call
# instead of going through the OpenAI `tool_calls` channel. Seen with
# owl-alpha (`<longcat_tool_call>...`) and various Claude/GPT fallbacks. When
# this fires AND `completion.tool_calls` is empty, the agent loop nudges the
# model back to structured mode and retries one round.
_TEXT_TOOL_CALL_PATTERNS: tuple[str, ...] = (
    r"<longcat_tool_call\b",
    r"</longcat_tool_call\b",
    r"<tool_call\b",
    r"</tool_call\b",
    r"<function_call\b",
    r"</function_call\b",
    r"```\s*tool_call\b",
    r"```\s*function_call\b",
)
_TEXT_TOOL_CALL_RE = re.compile("|".join(_TEXT_TOOL_CALL_PATTERNS), re.IGNORECASE)


def contains_text_tool_call(content: str | None) -> bool:
    """True when `content` carries text-shaped tool-call markup that should
    have come through the OpenAI `tool_calls` channel instead."""
    if not content:
        return False
    return _TEXT_TOOL_CALL_RE.search(content) is not None


# Strip patterns: cover paired tags, orphan opening tags, and fenced blocks.
_STRIP_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"<longcat_tool_call\b.*?</longcat_tool_call\s*>", re.DOTALL | re.IGNORECASE),
    (r"<tool_call\b.*?</tool_call\s*>", re.DOTALL | re.IGNORECASE),
    (r"<function_call\b.*?</function_call\s*>", re.DOTALL | re.IGNORECASE),
    (r"```\s*tool_call\b.*?```", re.DOTALL | re.IGNORECASE),
    (r"```\s*function_call\b.*?```", re.DOTALL | re.IGNORECASE),
    # Orphan opening tag → strip from there to end of message.
    (r"<longcat_tool_call\b.*", re.DOTALL | re.IGNORECASE),
    (r"<tool_call\b.*", re.DOTALL | re.IGNORECASE),
    (r"<function_call\b.*", re.DOTALL | re.IGNORECASE),
)


def strip_text_tool_calls(content: str | None) -> str:
    """Remove text-shaped tool-call markup from `content`.

    Used when loading prior assistant messages into history — we don't want
    the model to see its own (or a previous model's) text-tool-call attempts
    and copy the pattern. Returns the stripped content (may be empty)."""
    if not content:
        return ""
    out = content
    for pat, flags in _STRIP_PATTERNS:
        out = re.sub(pat, "", out, flags=flags)
    return out.strip()


_TEXT_TOOL_CALL_BLOCK = re.compile(
    r"<longcat_tool_call\b[^>]*>(.*?)</longcat_tool_call\s*>",
    re.DOTALL | re.IGNORECASE,
)
_KEY_VAL_PAIRS = re.compile(
    r"<longcat_arg_key\b[^>]*>(.*?)</longcat_arg_key\s*>"
    r"\s*"
    r"<longcat_arg_value\b[^>]*>(.*?)</longcat_arg_value\s*>",
    re.DOTALL | re.IGNORECASE,
)


def parse_text_tool_calls(content: str) -> list[dict]:
    """Parse `<longcat_tool_call>...</longcat_tool_call>` blocks into
    `{name, arguments}` dicts that the dispatch function can consume."""
    calls: list[dict] = []
    for block in _TEXT_TOOL_CALL_BLOCK.finditer(content):
        tool_body = block.group(1).strip()
        lines = tool_body.splitlines()
        name = lines[0].strip() if lines else ""
        args: dict[str, str] = {}
        for k_match, v_match in _KEY_VAL_PAIRS.findall(tool_body):
            args[k_match.strip()] = v_match.strip()
        if name:
            calls.append({"name": name, "arguments": args})
    return calls


_DRIFT_CORRECTION = (
    "Your previous turn contained a text-shaped tool call (e.g. "
    "<longcat_tool_call>... or a tool_call fenced block) but the function-"
    "calling channel is the only path that actually executes a tool. Retry "
    "the same intent using the structured tool_calls API — do not include "
    "tool-call markup in the message body."
)


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
    model: str = "openrouter/owl-alpha"
    max_tokens: int = 1024
    temperature: float = 0.2
    history_cap: int = HISTORY_TURN_CAP
    project_root: Path | None = None  # required for diff_apply
    diff_loop_enabled: bool = True
    agent_mode: str = "build"


def _system_prompt(rendered_context: str, mode: "AgentMode | None" = None) -> str:
    """Compose the per-turn system prompt.

    ``mode`` controls the agent persona:
      - BUILD (default): full access coding agent — opencode's "build"
      - PLAN: read-only analysis agent — opencode's "plan"

    Tools are advertised via the OpenAI `tools=[...]` schema on the request,
    not in the system text — duplicating them as prose competes with the
    structured channel and tempts function-calling models to emit text-shaped
    calls instead of native tool_calls. We just remind the model to use the
    function-calling channel when changing hub-owned state.
    """
    from .agent_tools import AgentMode

    if mode == AgentMode.PLAN:
        return _PLAN_SYSTEM_PROMPT(rendered_context)
    return _BUILD_SYSTEM_PROMPT(rendered_context)


def _BUILD_SYSTEM_PROMPT(rendered_context: str) -> str:
    return (
        "You are BAIRD, a bioinformatics research assistant in BUILD mode. "
        "You have full access to tools for reading/writing files, running "
        "commands on satellites, and managing projects. The active project's "
        "context follows. Be concise and silent while working: do not narrate "
        "your thought process, say what you're about to do, or add commentary "
        "between tool calls. Just execute the tools you need and present "
        "results in a structured format (tables, lists, fenced code blocks). "
        "When proposing code, give the change as a fenced unified diff so it "
        "can be reviewed and applied. For changes to hub-owned state (project "
        "locations, decisions, environment installs, satellite host.yaml) and "
        "for anything that needs to read files or run commands on a satellite, "
        "use the function-calling tools provided — do not invent your own "
        "tool-call syntax in the message body.\n\n"
        + rendered_context
    )


def _PLAN_SYSTEM_PROMPT(rendered_context: str) -> str:
    return (
        "You are BAIRD in PLAN mode — a read-only code exploration agent "
        "(opencode-style). Your goal is to understand the codebase, answer "
        "questions, and propose changes. You CANNOT write files, run "
        "destructive commands, or modify project state. You have access to "
        "safe read-only tools (read files, search, list projects). Be concise "
        "and focused: explain what you find, suggest approaches, but never "
        "execute write operations. The active project's context follows.\n\n"
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
    session_id: str | None = None,
) -> ReplStats:
    """Run the REPL until `/exit`, EOF, or the `inputs` iterable is exhausted.

    `inputs` is for tests — when supplied, lines are consumed from it instead
    of calling `input_fn`. EOF behaves like `/exit`.

    `session_id`, if provided, attaches to that specific session (re-loads its
    history) instead of the project's default `repl-<project_id>` session.
    """
    stats = ReplStats()
    diff_loop_active = config.diff_loop_enabled
    model_picker_cache: list[str] = []

    if session_id is not None:
        sessions = hub.list_sessions(project_id=config.project_id, limit=200)
        session = next((s for s in sessions if s["id"] == session_id), None)
        if session is None:
            raise RuntimeError(f"session {session_id} not found for project {config.project_id}")
    else:
        session = hub.new_session(
            mode="code",
            task_id=f"repl-{config.project_id}",
            project_id=config.project_id,
        )

    # Build epoch context (immutable baseline + change detection for sources).
    # Baseline goes into the system prompt; changed sources emit mid-conversation
    # system messages on subsequent turns.
    from .context_loader import build_epoch_context, reconcile_context
    epoch = build_epoch_context(repo_ctx)

    # Dynamic tool registry shared across the session.
    from .agent_tools import AgentMode, ToolRegistry
    tool_registry = ToolRegistry()
    agent_mode = AgentMode.BUILD if config.agent_mode == "build" else AgentMode.PLAN
    system = _system_prompt(epoch.baseline, mode=agent_mode)
    console.print(
        Panel.fit(
            f"[#C8102E]baird code[/#C8102E]  "
            f"project=[#1D70B8]{config.project_id}[/#1D70B8]  "
            f"model=[#6B7C93]{config.model}[/#6B7C93]\n"
            f"session={session['id'][:8]}  /help for commands, /exit to quit",
            border_style="#012169",
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

        # Multi-line support: a single line of `"""` opens a heredoc-style
        # block; another `"""` closes it. Lines collapsed into one message.
        if raw.strip() == '"""':
            buf: list[str] = []
            while True:
                try:
                    nxt = next(iterator) if iterator is not None else input_fn("... ")
                except (StopIteration, EOFError, KeyboardInterrupt):
                    break
                if nxt.strip() == '"""':
                    break
                buf.append(nxt.rstrip("\n"))
            raw = "\n".join(buf)

        line = raw.strip()
        if not line:
            continue

        if line.startswith("/"):
            # First chance to handle: the hub-first slash registry in
            # baird/slash.py (project/host/env/where/run on …). Returns None
            # when the line doesn't match a registered verb, in which case
            # the legacy REPL-internal commands below take a turn.
            from .agent_tools import ToolEnv, ToolRegistry
            from .slash import SlashContext, try_dispatch as _try_slash

            slash_ctx = SlashContext(
                hub=hub,
                env=ToolEnv(hub=hub, project_id=config.project_id),
                input_fn=input_fn if iterator is None else _iter_input_fn(iterator),
                console=console,
                active_host=getattr(config, "_active_host", None),
                tool_registry=tool_registry,
            )
            slash_res = _try_slash(line[1:], slash_ctx)
            if slash_res is not None:
                if slash_res.output:
                    style = "green" if slash_res.ok else "red"
                    console.print(f"[{style}]{slash_res.output}[/{style}]")
                if slash_res.active_host:
                    config._active_host = slash_res.active_host  # type: ignore[attr-defined]
                if slash_res.switch_to_project:
                    swapped = _switch_project(
                        slash_res.switch_to_project, hub, config, host_id, console
                    )
                    if swapped[0] is not None:
                        rendered, system, repo_ctx, session = swapped
                if slash_res.next_user_prompt:
                    # Fall through to the model-turn path with the primed
                    # prompt as the next user message (e.g. /audit-satellite).
                    line = slash_res.next_user_prompt
                else:
                    continue

            handed_off = False
            if slash_res is not None and slash_res.next_user_prompt:
                handed_off = True
            if not handed_off:
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
                if cmd in {"no-diff", "nodiff"}:
                    diff_loop_active = False
                    console.print("[yellow]diff prompts disabled for this session[/yellow]")
                    continue
                if cmd == "model":
                    parts = line.split(maxsplit=1)
                    arg = parts[1].strip() if len(parts) == 2 else ""
                    if not arg:
                        console.print(f"[dim]current model: {config.model}[/dim]")
                        try:
                            from .model import top_openrouter_models

                            picks = top_openrouter_models(n=20)
                            model_picker_cache = [m.get("id", "") for m in picks]
                            for i, m in enumerate(picks, 1):
                                console.print(
                                    f"  [cyan]{i:>2}.[/cyan] {m.get('id','')}"
                                )
                            console.print(
                                "[dim]usage: /model <number> or /model <full-id>[/dim]"
                            )
                        except Exception as e:
                            console.print(
                                f"[yellow]could not fetch model list ({e}); "
                                "you can still type /model <full-id>[/yellow]"
                            )
                    else:
                        new_model: str | None = None
                        if arg.isdigit() and model_picker_cache:
                            idx = int(arg)
                            if 1 <= idx <= len(model_picker_cache):
                                new_model = model_picker_cache[idx - 1]
                            else:
                                console.print(
                                    f"[red]index {idx} out of range[/red] "
                                    f"(1..{len(model_picker_cache)})"
                                )
                        else:
                            new_model = arg
                        if new_model:
                            old = config.model
                            config.model = new_model
                            console.print(f"[yellow]model:[/yellow] {old} → {new_model}")
                    continue
                if cmd == "project":
                    from .context_loader import lite_repo_context
                    from .project_yaml import ProjectYaml

                    parts = line.split()
                    if len(parts) == 1:
                        rows = hub.list_projects()
                        if not rows:
                            console.print("[dim]no projects on the hub[/dim]")
                        else:
                            for r in rows:
                                marker = " [green]*[/green]" if r["id"] == config.project_id else "  "
                                console.print(
                                    f"{marker} [cyan]{r['id']}[/cyan]  {r.get('name','')}"
                                )
                            console.print(
                                "[dim]switch: /project <id>   create: /project new <id> [name][/dim]"
                            )
                        continue
                    sub = parts[1]
                    if sub == "new":
                        if len(parts) < 3:
                            console.print("[red]usage:[/red] /project new <id> [name]")
                            continue
                        new_id = parts[2]
                        new_name = " ".join(parts[3:]) if len(parts) > 3 else new_id
                        try:
                            hub.upsert_project(id=new_id, name=new_name)
                        except Exception as e:
                            console.print(f"[red]create failed:[/red] {e}")
                            continue
                        console.print(f"[green]created project {new_id}[/green]")
                        target_id = new_id
                    else:
                        target_id = sub
                    try:
                        proj_row = hub.get_project(target_id)
                    except Exception as e:
                        console.print(f"[red]project '{target_id}' not on hub:[/red] {e}")
                        continue
                    py = ProjectYaml(
                        id=proj_row["id"],
                        name=proj_row.get("name") or proj_row["id"],
                        github=proj_row.get("github"),
                        context=proj_row.get("context"),
                        parent_id=proj_row.get("parent_id")
                        or (proj_row.get("config") or {}).get("parent_id"),
                    )
                    repo_ctx = lite_repo_context(py, hub=hub, host_id=host_id)
                    rendered = render_context(repo_ctx)
                    system = _system_prompt(rendered, mode=agent_mode)
                    config.project_id = target_id
                    config.project_root = None
                    session = hub.find_or_create_session_for_task(
                        task_id=f"repl-{target_id}",
                        project_id=target_id,
                        mode="code",
                    )
                    console.print(
                        f"[yellow]switched to project[/yellow] {target_id}  "
                        f"session={session['id'][:8]}"
                    )
                    continue
                if cmd == "sessions":
                    rows = hub.list_sessions(project_id=config.project_id, limit=20)
                    if not rows:
                        console.print("[dim]no prior sessions for this project[/dim]")
                    else:
                        console.print(f"[dim]sessions for {config.project_id}[/dim]")
                        for r in rows:
                            marker = " [green]*[/green]" if r["id"] == session["id"] else "  "
                            console.print(
                                f"{marker} {r['id'][:8]}  {r.get('mode','?')}  "
                                f"started={r.get('started_at','')[:19]}"
                            )
                        console.print(
                            "[dim]resume one with: baird code --session <full-id>[/dim]"
                        )
                    continue
                if cmd == "help":
                    from .slash import commands as _slash_cmds

                    console.print(
                        "[dim]/exit  /context  /reset  /cost  /model [id]  "
                        "/sessions  /project [id|new <id>]  /no-diff[/dim]"
                    )
                    console.print(
                        "[dim]hub-first: "
                        + "  ".join(f"/{c}" for c in _slash_cmds())
                        + "[/dim]"
                    )
                    continue
                console.print(f"[red]unknown command:[/red] /{cmd} (try /help)")
                continue

        def _on_tool_event(event: str, detail: str) -> None:
            if event == "call":
                console.print(f"[bold #C8102E]  $[/bold #C8102E] {detail}")
            elif event == "result":
                preview = detail.strip().splitlines()[0][:120] if detail else ""
                if preview:
                    console.print(f"[#6B7C93]{preview}[/#6B7C93]")
            elif event == "blocked":
                console.print(f"[bold #C8102E]  ![/bold #C8102E] {detail[:100]}")

        # Reconcile context sources that may have changed since last turn.
        # Injects any detected changes as system messages in the session so
        # they appear in the model's context before the user's message.
        if config.project_root is not None:
            try:
                from .context_loader import (
                    _git_log_oneline, _git_status, _build_tree,
                    GIT_LOG_SOURCE, GIT_STATUS_SOURCE, TREE_SOURCE,
                )
                fresh = RepoContext(
                    # Minimal — only the fields used by tracked sources
                    project=repo_ctx.project, project_root=config.project_root,
                    branch=repo_ctx.branch,
                    git_log_lines=_git_log_oneline(config.project_root, n=10),
                    git_status=_git_status(config.project_root),
                    tree=_build_tree(config.project_root),
                    relevant_files=repo_ctx.relevant_files,
                    decisions=repo_ctx.decisions,
                    action_summaries=repo_ctx.action_summaries,
                    rules_summary=repo_ctx.rules_summary,
                    host_id=repo_ctx.host_id,
                    locations=repo_ctx.locations,
                    parent=repo_ctx.parent,
                )
                updates = reconcile_context(epoch, fresh)
                for key, content in updates:
                    msg_text = f"[context update: {key}]\n{content}"
                    hub.append_message(session["id"], role="system", content=msg_text)
            except Exception:
                pass  # context update failure must not block the turn

        try:
            completion = _one_turn(
                user_msg=line,
                hub=hub,
                model_client=model_client,
                session_id=session["id"],
                config=config,
                system=system,
                host_id=host_id,
                tool_registry=tool_registry,
                agent_mode=agent_mode,
                on_tool_event=_on_tool_event,
            )
        except ModelError as e:
            console.print(f"[red]model error:[/red] {e}")
            continue

        if completion.content and not completion.tool_calls:
            console.print(completion.content)
        elif completion.content:
            console.print(f"[dim]{completion.content}[/dim]")
        console.print(
            f"[dim]model={completion.model}  "
            f"tokens={completion.usage.input_tokens}→{completion.usage.output_tokens}  "
            f"cost=${completion.cost_usd:.4f}[/dim]"
        )
        stats.turns += 1
        stats.total_cost_usd += completion.cost_usd
        stats.total_input_tokens += completion.usage.input_tokens
        stats.total_output_tokens += completion.usage.output_tokens

        if diff_loop_active and config.project_root is not None:
            _handle_diff_blocks(
                completion.content,
                console=console,
                project_root=config.project_root,
                input_fn=input_fn if iterator is None else _iter_input_fn(iterator),
            )

    console.print(
        f"[dim]session={session['id'][:8]}  turns={stats.turns}  total=${stats.total_cost_usd:.4f}[/dim]"
    )
    return stats


MAX_AGENT_ROUNDS = 12


def _one_turn(
    *,
    user_msg: str,
    hub: HubClient,
    model_client: OpenRouterClient,
    session_id: str,
    config: ReplConfig,
    system: str,
    host_id: str | None,
    tool_registry: ToolRegistry | None = None,
    agent_mode: AgentMode | None = None,
    on_chunk: Callable[[str], None] | None = None,
    on_tool_event: Callable[[str, str], None] | None = None,
) -> Completion:
    """Run one user-driven turn — including any agent loop the model triggers.

    The loop:
      1. Send user message + recent history (with the OpenAI-style `tools`
         schema derived from the hub tool catalogue).
      2. If the response has `tool_calls`, dispatch each via
         `agent_tools.dispatch`, append the assistant + tool result messages
         to the local history, and call the model again.
      3. Repeat until the model returns plain content (no tool calls) or we
         hit `MAX_AGENT_ROUNDS` (currently 12).

    Tier-3 (destructive) tool calls are blocked by default — they need
    explicit user approval via a slash command, not silent agent execution.
    The blocking is reported back to the model as a `tool` message so it can
    adapt rather than silently failing.

    Cost / tokens are accumulated across rounds and recorded on a single
    Action row. Every assistant message (including intermediate rounds with
    tool_calls) and every tool result is persisted to the session so the
    conversation history is fully replayable.

    Every provider call is streamed (tools are sent alongside the stream
    request, and tool_call deltas are accumulated from the stream). The
    `on_chunk` callback fires for every content delta and tool_call.

    `on_tool_event(event, detail)` is called for visibility: `event` is one
    of `call` / `result` / `blocked`.

    Tool calls from the same round are dispatched concurrently via
    `ThreadPoolExecutor` since they are independent.

    Git snapshots are captured before and after the turn; the delta is
    included in the tool event stream when `on_tool_event` is provided.
    """
    from .agent_tools import (
        AgentMode,
        ApprovalGate,
        ToolEnv,
        ToolRegistry,
        dispatch,
        tools_openai_schema,
    )
    from .context_compressor import load_history_with_summary

    catalogue = tool_registry.tools(agent_mode)
    tools_schema = tools_openai_schema(catalogue)
    env = ToolEnv(hub=hub, project_id=config.project_id)
    # Auto-run tier-1 + tier-2 calls; tier-3 (destructive) is rejected back to
    # the model so it can adapt. The user opts into destructive ops via the
    # corresponding slash commands.
    gate = ApprovalGate()

    # Capture pre-turn git snapshot for diffing after the turn.
    pre_snapshot = _capture_git_snapshot(config.project_root) if config.project_root else None

    with hub.start_action(
        project_id=config.project_id,
        tool_name="model",
        command="repl",
        host=host_id,
        model_name=config.model,
    ) as action:
        msgs = load_history_with_summary(
            hub,
            session_id=session_id,
            cap=config.history_cap,
            model_client=model_client,
            summary_model=config.model,
        )
        hub.append_message(session_id, role="user", content=user_msg)
        msgs.append({"role": "user", "content": user_msg})

        total_cost = 0.0
        total_input = 0
        total_output = 0
        completion: Completion | None = None
        _drift_corrected = False

        for round_idx in range(MAX_AGENT_ROUNDS):
            # Stream every round — tools are sent alongside the stream request
            # and tool_call deltas are accumulated from stream chunks.
            completion = model_client.stream_complete(
                model=config.model, messages=msgs,
                max_tokens=config.max_tokens, temperature=config.temperature,
                system=system, tools=tools_schema, on_chunk=on_chunk,
            )
            total_cost += completion.cost_usd
            total_input += completion.usage.input_tokens
            total_output += completion.usage.output_tokens

            # Build the structured assistant message dict
            assistant_msg: dict = {
                "role": "assistant",
                "content": completion.content or None,
            }
            if completion.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": _json.dumps(tc["arguments"]),
                        },
                    }
                    for tc in completion.tool_calls
                ]

            if not completion.tool_calls:
                # Self-healing: if the model emitted text-shaped tool-call
                # markup but didn't go through the structured channel, nudge
                # it back once and retry. This catches model drift (owl-alpha
                # has a known fallback to `<longcat_tool_call>...`) and
                # poisoned histories that escaped the load-side strip.
                if (
                    contains_text_tool_call(completion.content)
                    and not _drift_corrected
                ):
                    if on_tool_event:
                        on_tool_event(
                            "drift",
                            "text-shaped tool call detected — nudging model back "
                            "to the function-calling channel and retrying",
                        )
                    msgs.append({
                        "role": "assistant",
                        "content": strip_text_tool_calls(completion.content)
                                   or "(text-shaped tool call removed)",
                    })
                    msgs.append({"role": "system", "content": _DRIFT_CORRECTION})
                    _drift_corrected = True
                    continue

                # Fallback: execute text-shaped tool calls directly when the
                # model persists after the drift nudge.
                text_calls = parse_text_tool_calls(completion.content or "")
                if text_calls:
                    if on_tool_event:
                        on_tool_event(
                            "drift",
                            f"executing {len(text_calls)} text-shaped tool call(s)",
                        )
                    msgs.append({
                        "role": "assistant",
                        "content": strip_text_tool_calls(completion.content)
                                   or "(text-shaped tool call removed)",
                    })
                    for tc in text_calls:
                        result_str = _dispatch_tool_call(
                            {**tc, "id": f"text_{round_idx}_{tc['name']}"},
                            catalogue=catalogue, env=env, gate=gate,
                            on_tool_event=on_tool_event,
                        )
                        msgs.append({
                            "role": "tool",
                            "tool_call_id": f"text_{round_idx}_{tc['name']}",
                            "content": result_str,
                        })
                        hub.append_message(
                            session_id, role="tool",
                            content=result_str,
                            tool_call_id=f"text_{round_idx}_{tc['name']}",
                        )
                    continue

                # Persist the final assistant message (no tool_calls) and break.
                hub.append_message(
                    session_id, role="assistant", content=completion.content
                )
                break

            # --- Round with tool_calls ---

            # Persist assistant message with tool_calls
            msgs.append(assistant_msg)
            hub.append_message(
                session_id, role="assistant", content=completion.content,
                tool_calls=assistant_msg["tool_calls"],
            )

            # Dispatch tool calls concurrently since they are independent.
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _run_one(tc: dict) -> tuple[str, str]:
                result = _dispatch_tool_call(
                    tc, catalogue=catalogue, env=env, gate=gate,
                    on_tool_event=on_tool_event,
                )
                return tc["id"], result

            results: dict[str, str] = {}
            with ThreadPoolExecutor(max_workers=len(completion.tool_calls)) as pool:
                futures = {pool.submit(_run_one, tc): tc for tc in completion.tool_calls}
                for f in as_completed(futures):
                    tc_id, result_str = f.result()
                    results[tc_id] = result_str

            # Append tool results in original order for deterministic history
            for tc in completion.tool_calls:
                result_str = results[tc["id"]]
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_str,
                })
                hub.append_message(
                    session_id, role="tool",
                    content=result_str,
                    tool_call_id=tc["id"],
                )

        # Capture post-turn git snapshot and report file changes.
        post_snapshot = _capture_git_snapshot(config.project_root) if config.project_root else None
        file_delta = _diff_snapshots(pre_snapshot, post_snapshot)
        if file_delta and on_tool_event:
            on_tool_event("files", file_delta)

        # Final completion exists; record aggregated usage.
        assert completion is not None
        action.record_usage(
            cost_usd=total_cost,
            input_tokens=total_input,
            output_tokens=total_output,
        )
        first_line = completion.content.strip().splitlines()[0] if completion.content else ""
        action.set_summary(first_line[:200])

    # Surface the aggregate totals on the returned Completion so the REPL
    # footer shows the whole loop, not just the last round.
    completion.cost_usd = total_cost
    completion.usage.input_tokens = total_input
    completion.usage.output_tokens = total_output
    return completion


def _dispatch_tool_call(
    tc: dict[str, object],
    *,
    catalogue,
    env,
    gate,
    on_tool_event: Callable[[str, str], None] | None,
) -> str:
    """Run one tool call from the model. Returns a string to feed back as the
    `tool` message content. Errors are stringified rather than raised, so the
    model can see what went wrong and adapt."""
    from .agent_tools import classify_tool_call, dispatch

    name = str(tc.get("name") or "")
    args = tc.get("arguments") or {}
    if name not in catalogue:
        if on_tool_event:
            on_tool_event("blocked", f"unknown tool: {name}")
        return f"error: unknown tool {name!r}"
    tool = catalogue[name]

    if on_tool_event:
        on_tool_event("call", f"{name}({_format_args(args)})")

    # Pre-classify: tier-3 destructive calls are not auto-run from inside the
    # agent loop. The user must promote via a slash command.
    try:
        decision = classify_tool_call(tool, args)
    except Exception:
        decision = None
    if decision is not None and decision.tier.value == "destructive":
        if on_tool_event:
            on_tool_event("blocked", f"{name}: tier-3 (destructive) — not auto-run")
        return (
            f"BLOCKED (tier 3 / destructive): {decision.reason}. "
            "Ask the user to run this via the matching slash command instead."
        )

    try:
        result = dispatch(tool, dict(args), env, gate=gate)
    except Exception as e:
        if on_tool_event:
            on_tool_event("result", f"{name}: exception {e}")
        return f"exception: {e}"
    if not result.ok:
        if on_tool_event:
            on_tool_event("result", f"{name}: error {result.error}")
        return f"error: {result.error}"
    out = _format_tool_result(result.result)
    if on_tool_event:
        summary = out if len(out) < 200 else out[:200] + "…"
        on_tool_event("result", f"{name}: {summary}")
    return out


def _format_args(args: object) -> str:
    if not isinstance(args, dict):
        return repr(args)
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "…"
        parts.append(f"{k}={s}")
    return ", ".join(parts)


TOOL_RESULT_MAX_CHARS = 8000


def _format_tool_result(result: object) -> str:
    if result is None:
        return "ok"
    if isinstance(result, (str, int, float, bool)):
        return str(result)[:TOOL_RESULT_MAX_CHARS]
    try:
        return _json.dumps(result, default=str)[:TOOL_RESULT_MAX_CHARS]
    except Exception:
        return str(result)[:TOOL_RESULT_MAX_CHARS]


def _switch_project(target_id, hub, config, host_id, console):
    """Reload context/session for `target_id`. Used by /project switch and by
    slash commands that signal `switch_to_project` (e.g. /project new)."""
    from .agent_tools import AgentMode
    from .context_loader import lite_repo_context
    from .project_yaml import ProjectYaml

    try:
        proj_row = hub.get_project(target_id)
    except Exception as e:
        console.print(f"[red]project '{target_id}' not on hub:[/red] {e}")
        return None, None, None, None
    py = ProjectYaml(
        id=proj_row["id"],
        name=proj_row.get("name") or proj_row["id"],
        github=proj_row.get("github"),
        context=proj_row.get("context"),
        parent_id=proj_row.get("parent_id")
        or (proj_row.get("config") or {}).get("parent_id"),
    )
    repo_ctx = lite_repo_context(py, hub=hub, host_id=host_id)
    rendered = render_context(repo_ctx)
    system = _system_prompt(rendered, mode=AgentMode(getattr(config, "agent_mode", "build")))
    config.project_id = target_id
    config.project_root = None
    session = hub.find_or_create_session_for_task(
        task_id=f"repl-{target_id}",
        project_id=target_id,
        mode="code",
    )
    console.print(
        f"[yellow]switched to project[/yellow] {target_id}  "
        f"session={session['id'][:8]}"
    )
    return rendered, system, repo_ctx, session


def _iter_input_fn(iterator: Iterable[str]) -> Callable[[str], str]:
    """Adapt an `inputs=` iterable so the diff prompt can read the next line.

    Returns "" on exhaustion (treated as "no apply") — the prompt skips.
    """
    def _read(_: str) -> str:
        try:
            return next(iterator)  # type: ignore[arg-type]
        except StopIteration:
            return ""

    return _read


def _handle_diff_blocks(
    content: str,
    *,
    console: Console,
    project_root: Path,
    input_fn: Callable[[str], str],
) -> None:
    """Find fenced diff blocks in `content` and prompt the user per block."""
    blocks = extract_diff_blocks(content)
    if not blocks:
        return
    for i, diff in enumerate(blocks, 1):
        console.print(
            Panel(
                Syntax(diff, "diff", line_numbers=False, word_wrap=True),
                title=f"proposed diff {i}/{len(blocks)}",
                border_style="cyan",
            )
        )
        try:
            choice = input_fn("apply? [y/N/q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if choice == "q":
            return
        if choice != "y":
            console.print("[dim]skipped[/dim]")
            continue
        try:
            result = apply_diff_to_repo(
                repo=project_root,
                diff_text=diff,
                commit_message=f"baird: apply REPL-proposed diff {i}",
                action_id=f"repl-{uuid.uuid4().hex[:8]}",
            )
        except DiffApplyError as e:
            console.print(f"[red]apply failed:[/red] {e}")
            continue
        console.print(
            f"[green]applied[/green] {result.commit_sha[:12]} "
            f"({len(result.files_changed)} file(s))"
        )
