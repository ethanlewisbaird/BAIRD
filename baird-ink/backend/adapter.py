"""
JSON-stream REPL adapter — runs the BAIRD backend and communicates with the Ink
frontend via newline-delimited JSON on stdin/stdout.

Protocol:
  Frontend → stdin:  {"command": "input", "text": "..."}
                      {"command": "dialog", "choice": "..."}
                      {"command": "exit"}
  Backend  → stdout: {"kind": "event_type", ...}  (one JSON object per line)

This is the ONLY new Python file. The existing backend (baird/) is imported,
not modified.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

# Ensure the BAIRD root is importable (../.. from backend/adapter.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def _main() -> None:
    import time

    from baird.config import load_host_config
    from baird.memory_client import HubClient
    from baird.model import OpenRouterClient, make_hub_proxy_transport
    from baird.repl import ReplConfig, ReplStats, _one_turn, _system_prompt
    from baird.context_loader import load_repo_context, build_epoch_context
    from baird.agent_tools import AgentMode, ToolRegistry

    # ── Setup (mirrors cli.py) ──
    root = Path.cwd()
    has_local = (root / ".baird" / "project.yaml").exists()

    def _hub() -> HubClient:
        from baird import paths as _paths
        from baird.supervisor import ensure_hub_running
        ensure_hub_running()
        host_path = _paths.host_yaml_path()
        if host_path.exists():
            cfg = load_host_config(host_path)
            return HubClient(cfg.hub_url, cfg.effective_hub_token())
        from baird.config import load_hub_config
        hub_cfg = load_hub_config()
        host, port = hub_cfg.listen.split(":")
        return HubClient(f"http://{host}:{port}", hub_cfg.auth_token)

    hub = _hub()

    if has_local:
        ctx = load_repo_context(root, hub=hub)
    else:
        hub.upsert_project(id="scratch", name="Scratch", context="Ad-hoc work.")
        from baird.context_loader import lite_repo_context
        from baird.project_yaml import ProjectYaml
        proj_row = hub.get_project("scratch")
        py = ProjectYaml(id="scratch", name="Scratch", context=proj_row["context"])
        ctx = lite_repo_context(py, hub=hub)

    # Transport
    from baird import paths as _paths
    transport = None
    host_path = _paths.host_yaml_path()
    if host_path.exists():
        cfg = load_host_config(host_path)
        if cfg.use_hub_for_models:
            transport = make_hub_proxy_transport(
                hub_url=cfg.hub_url,
                auth_token=cfg.effective_hub_token(),
            )

    model_client = OpenRouterClient(transport=transport)

    config = ReplConfig(
        project_id=ctx.project.id,
        project_root=ctx.project_root,
    )

    # Session
    session = hub.new_session(
        mode="code",
        task_id=f"repl-{config.project_id}",
        project_id=config.project_id,
    )

    epoch = build_epoch_context(ctx)
    tool_registry = ToolRegistry()
    agent_mode = AgentMode.BUILD
    system = _system_prompt(epoch.baseline, mode=agent_mode)
    stats = ReplStats()

    # ── Helpers ──
    seen_tool_names: set[str] = set()

    def _emit(obj: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    # ── Emit session info ──
    _emit({
        "kind": "model_info",
        "model": config.model,
        "agentMode": agent_mode.value,
    })
    _emit({
        "kind": "status",
        "text": f"session={session['id'][:8]}  project={config.project_id}  model={config.model}",
    })

    # ── Session info also emitted via first events ──
    _emit({
        "kind": "stats_update",
        "turns": 0,
        "costUsd": 0.0,
        "inputTokens": 0,
        "outputTokens": 0,
    })

    # ── Tool call ID tracking ──
    # We generate sequential IDs (tc_name, tc_name_2, ...) since the
    # on_tool_event callback only carries the tool name, not the API call ID.
    # This ensures consistency between tool_call_begin and subsequent events.
    _tool_call_counts: dict[str, int] = {}
    _pending_start_ids: list[str] = []

    def _tool_id(name: str) -> str:
        count = _tool_call_counts.get(name, 0) + 1
        _tool_call_counts[name] = count
        return f"tc_{name}" if count == 1 else f"tc_{name}_{count}"

    def _on_chunk(delta: str) -> None:
        nonlocal _tool_call_counts, _pending_start_ids
        if delta.startswith('{"tool_calls":'):
            try:
                tc_list = json.loads(delta).get("tool_calls", [])
                for tc in tc_list:
                    fn = tc.get("function", tc)
                    name = fn.get("name", "?")
                    if name not in seen_tool_names:
                        seen_tool_names.add(name)
                        args_raw = fn.get("arguments", {})
                        args_str = (
                            json.dumps(args_raw)
                            if isinstance(args_raw, dict)
                            else str(args_raw)
                        )
                        tc_id = _tool_id(name)
                        _pending_start_ids.append(tc_id)
                        _emit({
                            "kind": "tool_call_begin",
                            "id": tc_id,
                            "name": name,
                            "arguments": args_str[:100],
                        })
            except Exception:
                pass
        else:
            _emit({"kind": "text_delta", "delta": delta})

    _last_started_id: dict[str, str] = {}

    def _on_tool_event(event: str, detail: str) -> None:
        nonlocal _pending_start_ids, _last_started_id
        if event == "call":
            name = detail.split("(")[0]
            tc_id = _pending_start_ids.pop(0) if _pending_start_ids else f"tc_{name}"
            _last_started_id[name] = tc_id
            _emit({"kind": "tool_started", "invocationId": tc_id})
        elif event == "result":
            name = detail.split(":")[0]
            tc_id = _last_started_id.get(name, f"tc_{name}")
            content = detail.strip()
            if ":" in (content.splitlines()[0] if content else ""):
                _, _, rest = content.partition(": ")
                content = rest
            _emit({"kind": "tool_completed", "invocationId": tc_id})
            _emit({
                "kind": "tool_output",
                "invocationId": tc_id,
                "chunk": content[:500],
            })
        elif event == "blocked":
            _emit({"kind": "error", "text": f"blocked: {detail[:80]}"})
        elif event == "files":
            _emit({"kind": "status", "text": f"files changed: {detail[:200]}"})

    def _input_fn(prompt: str) -> str:
        """Read a line from stdin JSON commands."""
        raw = sys.stdin.readline()
        if not raw:
            raise EOFError("stdin closed")
        try:
            cmd = json.loads(raw.strip())
            if cmd.get("command") == "input":
                return cmd.get("text", "")
            if cmd.get("command") == "dialog":
                return cmd.get("choice", "")
            if cmd.get("command") == "exit":
                raise EOFError("exit command")
        except json.JSONDecodeError:
            return raw.strip()
        return ""

    # ── Main REPL loop ──
    diff_loop_active = True

    while True:
        try:
            raw = _input_fn("")
        except (EOFError, KeyboardInterrupt):
            _emit({"kind": "dialog_dismiss"})
            break

        line = raw.strip()
        if not line:
            continue

        # ── Slash commands ──
        if line.startswith("/"):
            cmd = line[1:].split()[0].lower()
            if cmd in {"exit", "quit"}:
                _emit({"kind": "dialog_dismiss"})
                break
            if cmd == "reset":
                session = hub.new_session(
                    mode="code",
                    project_id=config.project_id,
                    task_id=f"repl-{config.project_id}",
                )
                _emit({"kind": "status", "text": f"session reset: {session['id'][:8]}"})
                continue
            if cmd == "cost":
                _emit({
                    "kind": "status",
                    "text": f"turns={stats.turns}  cost=${stats.total_cost_usd:.4f}  "
                            f"tokens={stats.total_input_tokens}→{stats.total_output_tokens}",
                })
                continue
            # Fall through — unknown commands are passed to the model as input

        # ── User message ──
        _emit({"kind": "user_message", "text": line})
        _emit({"kind": "turn_start"})
        seen_tool_names.clear()

        try:
            completion = _one_turn(
                user_msg=line,
                hub=hub,
                model_client=model_client,
                session_id=session["id"],
                config=config,
                system=system,
                host_id=None,
                tool_registry=tool_registry,
                agent_mode=agent_mode,
                on_chunk=_on_chunk,
                on_tool_event=_on_tool_event,
            )
        except Exception as e:
            _emit({"kind": "error", "text": str(e)})
            continue

        # ── Finalize turn ──
        stats.turns += 1
        stats.total_cost_usd += completion.cost_usd
        stats.total_input_tokens += completion.usage.input_tokens
        stats.total_output_tokens += completion.usage.output_tokens

        _emit({
            "kind": "stream_end",
            "usage": {
                "inputTokens": completion.usage.input_tokens,
                "outputTokens": completion.usage.output_tokens,
                "costUsd": completion.cost_usd,
            },
        })
        _emit({
            "kind": "stats_update",
            "turns": stats.turns,
            "costUsd": stats.total_cost_usd,
            "inputTokens": stats.total_input_tokens,
            "outputTokens": stats.total_output_tokens,
        })

        # ── Diff blocks ──
        if diff_loop_active and config.project_root is not None:
            from baird.diff_apply import DiffApplyError, apply_diff_to_repo
            from baird.repl import extract_diff_blocks

            blocks = extract_diff_blocks(completion.content or "")
            for i, diff in enumerate(blocks, 1):
                preview = diff.splitlines()
                preview_text = preview[0] if preview else ""
                _emit({
                    "kind": "dialog",
                    "id": f"diff_{i}",
                    "title": f"Diff {i}/{len(blocks)}",
                    "body": f"Apply diff block? [{preview_text}]\n(d)iff view  (y)es  (n)o  (e)dit  (q)uit",
                    "choices": ["y", "n", "q"],
                })
                try:
                    choice = _input_fn("") or "n"
                except EOFError:
                    break
                if choice == "q":
                    break
                if choice in ("y", ""):
                    try:
                        result = apply_diff_to_repo(
                            repo=config.project_root,
                            diff_text=diff,
                            commit_message=f"baird: apply diff {i}",
                            action_id=f"repl-{os.urandom(4).hex()}",
                        )
                        _emit({
                            "kind": "status",
                            "text": f"applied {result.commit_sha[:12]} ({len(result.files_changed)} file(s))",
                        })
                    except DiffApplyError as e:
                        _emit({"kind": "error", "text": f"apply failed: {e}"})
                else:
                    _emit({"kind": "status", "text": "skipped"})

    _emit({
        "kind": "status",
        "text": f"session={session['id'][:8]}  turns={stats.turns}  total=${stats.total_cost_usd:.4f}",
    })


if __name__ == "__main__":
    _main()
