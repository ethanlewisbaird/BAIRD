"""
JSON-stream REPL adapter — runs the BAIRD backend and communicates with the Ink
frontend via newline-delimited JSON on stdin/stdout.

Protocol:
  Frontend -> stdin:  {"command": "input", "text": "..."}
                      {"command": "dialog", "choice": "..."}
                      {"command": "exit"}
  Backend  -> stdout: {"kind": "event_type", ...}  (one JSON object per line)

This is the ONLY new Python file. The existing backend (baird/) is imported,
not modified.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def _main() -> None:
    import time

    # Load secrets.env into environment at startup (so keys persist across sessions)
    from baird.paths import secrets_env_path as _secrets_env_path
    _senv = _secrets_env_path()
    if _senv.exists():
        for _line in _senv.read_text().splitlines():
            _line = _line.strip()
            if _line and "=" in _line and not _line.startswith("#"):
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

    from baird.config import load_host_config
    from baird.memory_client import HubClient
    from baird.model import OpenRouterClient, make_hub_proxy_transport
    from baird.repl import ReplConfig, ReplStats, _one_turn, _system_prompt
    from baird.context_loader import load_repo_context, build_epoch_context
    from baird.agent_tools import AgentMode, ToolRegistry

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
    _model_picker_cache: list[str] = []

    def _emit(obj: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    _emit({"kind": "model_info", "model": config.model, "agentMode": agent_mode.value})
    _emit({"kind": "status", "text": f"session={session['id'][:8]}  project={config.project_id}  model={config.model}"})
    _emit({"kind": "stats_update", "turns": 0, "costUsd": 0.0, "inputTokens": 0, "outputTokens": 0})

    _tool_call_counts: dict[str, int] = {}
    _pending_start_ids: list[str] = []
    _last_started_id: dict[str, str] = {}

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
                        args_str = json.dumps(args_raw) if isinstance(args_raw, dict) else str(args_raw)
                        tc_id = _tool_id(name)
                        _pending_start_ids.append(tc_id)
                        _emit({"kind": "tool_call_begin", "id": tc_id, "name": name, "arguments": args_str[:100]})
            except Exception:
                pass
        else:
            _emit({"kind": "text_delta", "delta": delta})

    # Track last output to avoid repeating identical tool results
    _last_output: str = ""
    _tool_result_count: dict[str, int] = {}

    def _format_output(content: str, tool_name: str = "") -> str | None:
        """Return a one-line summary of tool output, or None to skip."""
        nonlocal _last_output, _tool_result_count
        if not content or content.strip() in ("[]", "{}", "ok", "(empty stdout)"):
            return None
        # Limit repetitive results per tool (at most 3 shown)
        cnt = _tool_result_count.get(tool_name, 0) + 1
        _tool_result_count[tool_name] = cnt
        if cnt > 2:
            return None
        # Try to parse as JSON — some results have embedded newlines, fix those
        fixed = content
        try:
            json.loads(fixed)
        except (json.JSONDecodeError, TypeError):
            # Escape unescaped control chars inside JSON strings
            in_str = False
            chars = list(fixed)
            for i, c in enumerate(chars):
                if c == '"':
                    in_str = not in_str
                elif in_str and c in '\n\r\t':
                    chars[i] = {'\n': '\\n', '\r': '\\r', '\t': '\\t'}.get(c, c)
            fixed = ''.join(chars)
        try:
            obj = json.loads(fixed)
            # Command result with stdout
            if isinstance(obj, dict) and "stdout" in obj:
                out = obj.get("stdout", "") or ""
                if out:
                    first = out.strip().split("\n")[0][:120]
                    return first
                err = obj.get("stderr", "") or ""
                if err:
                    return err.strip().split("\n")[0][:120]
                return None
            # Project list
            if isinstance(obj, list) and len(obj) > 0 and isinstance(obj[0], dict) and "id" in obj[0]:
                names = ", ".join(item.get("name", item["id"]) for item in obj[:5])
                tail = f" (+{len(obj) - 5})" if len(obj) > 5 else ""
                return f"{len(obj)} project(s): {names}{tail}"
            # Location list
            if isinstance(obj, list) and len(obj) > 0 and isinstance(obj[0], dict) and "host" in obj[0]:
                locs = ", ".join(item.get("path", "").split("/")[-1] for item in obj[:5])
                tail = f" (+{len(obj) - 5})" if len(obj) > 5 else ""
                return f"{len(obj)} location(s): {locs}{tail}"
            # Single location
            if isinstance(obj, dict) and "host" in obj and "path" in obj:
                return f"{obj.get('host','?')}:{obj.get('path','?')} ({obj.get('role','?')})"
            # Other dict/list — compact
            if isinstance(obj, list):
                return f"[{len(obj)} item(s)]"
            if isinstance(obj, dict):
                keys = ", ".join(sorted(obj.keys())[:4])
                return f"{{{keys}}}"
            return str(obj)[:120]
        except (json.JSONDecodeError, TypeError):
            pass
        # Fallback: first line of raw content
        first_line = content.splitlines()[0][:120]
        return first_line

    def _on_tool_event(event: str, detail: str) -> None:
        nonlocal _pending_start_ids, _last_started_id, _last_output, _tool_result_count
        if event == "call":
            name = detail.split("(")[0]
            tc_id = _pending_start_ids.pop(0) if _pending_start_ids else f"tc_{name}"
            _last_started_id[name] = tc_id
            _emit({"kind": "tool_started", "invocationId": tc_id})
        elif event == "result":
            name = detail.split(":")[0]
            tc_id = _last_started_id.get(name, f"tc_{name}")
            content = detail.strip()
            if content.startswith(name + ":"):
                content = content[len(name) + 1:].strip()
            _emit({"kind": "tool_completed", "invocationId": tc_id})
            formatted = _format_output(content, name)
            if formatted is not None:
                # Skip if identical to last output (deduplicate)
                if formatted == _last_output:
                    return
                _last_output = formatted
                _emit({"kind": "tool_output", "invocationId": tc_id, "chunk": formatted})
        elif event == "blocked":
            _emit({"kind": "error", "text": f"blocked: {detail[:80]}"})
        elif event == "files":
            _emit({"kind": "status", "text": f"files changed: {detail[:200]}"})

    def _input_fn(prompt: str) -> str:
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

    # ── Slash command helpers ──

    def _cmd_model(args: list[str]) -> None:
        nonlocal _model_picker_cache
        from baird.model import top_openrouter_models
        if not args:
            _emit({"kind": "status", "text": f"current model: {config.model}"})
            try:
                picks = top_openrouter_models(n=20)
                _model_picker_cache = [m.get("id", "") for m in picks]
                lines = [f"  {i:>2}. {m.get('id','')}" for i, m in enumerate(picks, 1)]
                lines.append("usage: /model <number> or /model <full-id>")
                _emit({"kind": "status", "text": "\n".join(lines)})
            except Exception as e:
                _emit({"kind": "warning", "text": f"could not fetch model list ({e})"})
            return
        arg = args[0]
        new_model: str | None = None
        if arg.isdigit() and _model_picker_cache:
            idx = int(arg)
            if 1 <= idx <= len(_model_picker_cache):
                new_model = _model_picker_cache[idx - 1]
        if not new_model:
            new_model = arg
        old = config.model
        config.model = new_model
        _emit({"kind": "status", "text": f"model: {old} -> {new_model}"})
        _emit({"kind": "model_info", "model": config.model, "agentMode": agent_mode.value})

    def _cmd_reset() -> None:
        nonlocal session
        session = hub.new_session(mode="code", task_id=f"repl-{config.project_id}", project_id=config.project_id)
        _emit({"kind": "status", "text": f"session reset: {session['id'][:8]}"})

    def _cmd_cost() -> None:
        _emit({"kind": "status", "text": f"turns={stats.turns}  cost=${stats.total_cost_usd:.4f}  tokens={stats.total_input_tokens}->{stats.total_output_tokens}"})

    def _cmd_help() -> None:
        _emit({"kind": "status", "text": "/exit  /context  /reset  /cost  /model [id]  /mode [build|plan|auto]  /sessions  /project [id|new <id>]  /connect [--file <path>]  /help"})

    def _cmd_sessions() -> None:
        rows = hub.list_sessions(project_id=config.project_id, limit=20)
        if not rows:
            _emit({"kind": "status", "text": "no prior sessions for this project"})
            return
        lines = [f"sessions for {config.project_id}"]
        for r in rows:
            marker = "*" if r["id"] == session["id"] else " "
            lines.append(f" {marker} {r['id'][:8]}  {r.get('mode','?')}  started={r.get('started_at','')[:19]}")
        _emit({"kind": "status", "text": "\n".join(lines)})

    def _cmd_project(args: list[str]) -> None:
        nonlocal system, ctx, session, config
        if not args:
            rows = hub.list_projects()
            if not rows:
                _emit({"kind": "status", "text": "no projects on the hub"})
                return
            lines = []
            for r in rows:
                marker = "*" if r["id"] == config.project_id else " "
                lines.append(f" {marker} {r['id']}  {r.get('name','')}")
            lines.append("switch: /project <id>   create: /project new <id> [name]")
            _emit({"kind": "status", "text": "\n".join(lines)})
            return

        sub = args[0]
        if sub == "new":
            if len(args) < 2:
                _emit({"kind": "status", "text": "usage: /project new <id> [name]"})
                return
            new_id = args[1]
            new_name = " ".join(args[2:]) if len(args) > 2 else new_id
            try:
                hub.upsert_project(id=new_id, name=new_name)
            except Exception as e:
                _emit({"kind": "error", "text": f"create failed: {e}"})
                return
            _emit({"kind": "status", "text": f"created project {new_id}"})
            target_id = new_id
        else:
            target_id = sub

        try:
            proj_row = hub.get_project(target_id)
        except Exception as e:
            _emit({"kind": "error", "text": f"project '{target_id}' not on hub: {e}"})
            return
        from baird.project_yaml import ProjectYaml
        from baird.context_loader import lite_repo_context
        py = ProjectYaml(
            id=proj_row["id"],
            name=proj_row.get("name") or proj_row["id"],
            github=proj_row.get("github"),
            context=proj_row.get("context"),
            parent_id=proj_row.get("parent_id") or (proj_row.get("config") or {}).get("parent_id"),
        )
        new_ctx = lite_repo_context(py, hub=hub)
        rendered = _render_context(new_ctx)
        ctx = new_ctx
        system = _system_prompt(rendered, mode=agent_mode)
        config.project_id = target_id
        config.project_root = None
        session = hub.find_or_create_session_for_task(task_id=f"repl-{target_id}", project_id=target_id, mode="code")
        _emit({"kind": "status", "text": f"switched to project {target_id}  session={session['id'][:8]}"})

    def _save_connect_key(provider: tuple[str, str, str], key: str) -> None:
        label, env_var, _url = provider
        if not key.strip():
            _emit({"kind": "error", "text": "no key provided"})
            return
        from baird.paths import secrets_env_path
        env_path = secrets_env_path()
        env_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, str] = {}
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()
        existing[env_var] = key.strip()
        env_path.write_text("\n".join(f"{k}={v}" for k, v in sorted(existing.items())) + "\n")
        env_path.chmod(0o600)
        os.environ[env_var] = key.strip()
        _emit({"kind": "status", "text": f"connected to {label} — key saved"})

    def _cmd_connect(args: list[str] | None = None) -> None:
        providers = [
            ("OpenRouter", "OPENROUTER_API_KEY", "https://openrouter.ai/keys"),
            ("OpenCode Zen", "OPENCODE_API_KEY", "https://opencode.ai/auth (free tier)"),
            ("OpenCode Go", "OPENCODE_API_KEY", "https://opencode.ai/auth (subscription)"),
        ]

        # Read key from file: /connect [number] --file <path>
        if args and '--file' in args:
            file_idx = args.index('--file')
            if file_idx + 1 < len(args):
                filepath = args[file_idx + 1]
                provider_idx = 2  # default: OpenCode Go
                if file_idx >= 1 and args[0].isdigit():
                    provider_idx = int(args[0]) - 1
                if 0 <= provider_idx < len(providers):
                    try:
                        key = Path(filepath).expanduser().read_text().strip()
                        if key:
                            _save_connect_key(providers[provider_idx], key)
                            return
                    except OSError as e:
                        _emit({"kind": "error", "text": f"read failed: {e}"})
                        return
                _emit({"kind": "error", "text": "no key found in file"})
                return
            _emit({"kind": "error", "text": "usage: /connect [number] --file <path>"})
            return

        # Non-interactive: /connect <number> <key>
        if args and len(args) >= 2 and args[0].isdigit():
            idx = int(args[0]) - 1
            if 0 <= idx < len(providers):
                _save_connect_key(providers[idx], " ".join(args[1:]))
                return
            _emit({"kind": "error", "text": f"invalid provider: {args[0]}"})
            return

        _emit({
            "kind": "dialog",
            "id": "connect_provider",
            "title": "Connect API Provider",
            "body": "Select a provider:\n" + "\n".join(f"  {i+1}. {label} ({url})" for i, (label, _, url) in enumerate(providers)),
            "choices": [label for label, _, _ in providers],
        })
        try:
            raw = _input_fn("")
        except EOFError:
            return
        try:
            idx = int(raw.strip()) - 1
            if idx < 0 or idx >= len(providers):
                _emit({"kind": "error", "text": "invalid selection"})
                return
        except ValueError:
            _emit({"kind": "error", "text": "invalid selection"})
            return

        label, env_var, url = providers[idx]
        _emit({
            "kind": "dialog",
            "id": "connect_key",
            "title": f"Connect {label}",
            "body": f"Get your API key from: {url}\nPaste your {label} API key below:",
            "choices": [],
        })
        try:
            key = _input_fn("")
        except EOFError:
            return
        key = key.strip()
        if not key:
            _emit({"kind": "error", "text": "no key provided — cancelled"})
            return

        _save_connect_key((label, env_var, url), key)

    # ── Slash dispatch wrapper ──
    # Rich Console that renders to a string buffer, which we then emit as JSON
    import io as _io
    from rich.console import Console as _RichConsole

    _capture_buffer = _io.StringIO()
    _capture_console = _RichConsole(file=_capture_buffer, force_terminal=False, highlight=False, no_color=True)

    def _emit_captured() -> None:
        text = _capture_buffer.getvalue()
        _capture_buffer.truncate(0)
        _capture_buffer.seek(0)
        if text.strip():
            _emit({"kind": "status", "text": text.strip()})

    # Dialog-based input_fn for interactive slash commands.
    # When a slash command calls input_fn(prompt), we emit a text-input dialog
    # and wait for the response from stdin.
    _dialog_id_counter: int = 0

    def _dialog_input_fn(prompt: str) -> str:
        nonlocal _dialog_id_counter
        _dialog_id_counter += 1
        _emit_captured()  # flush any buffered console output before the prompt
        _emit({
            "kind": "dialog",
            "id": f"slash_{_dialog_id_counter}",
            "title": prompt.strip().rstrip(":").rstrip(),
            "body": prompt,
            "choices": [],
        })
        try:
            return _input_fn("")
        except EOFError:
            return ""

    # ── Main loop ──
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

        if line.startswith("/"):
            cmd = line[1:].split()[0].lower()
            rest = line[len(cmd) + 2:].strip().split()
            if cmd in ("exit", "quit"):
                _emit({"kind": "dialog_dismiss"})
                break
            elif cmd == "reset":
                _cmd_reset()
                continue
            elif cmd == "cost":
                _cmd_cost()
                continue
            elif cmd == "help":
                _cmd_help()
                continue
            elif cmd == "sessions":
                _cmd_sessions()
                continue
            elif cmd == "model":
                _cmd_model(rest)
                continue
            elif cmd == "mode":
                if rest and rest[0].lower() in ("build", "plan", "auto"):
                    agent_mode = AgentMode(rest[0].lower())
                else:
                    agent_mode = agent_mode.toggle()
                system = _system_prompt(epoch.baseline, mode=agent_mode)
                _emit({"kind": "model_info", "model": config.model, "agentMode": agent_mode.value})
                _emit({"kind": "status", "text": f"switched to {agent_mode.badge}"})
                continue
            elif cmd == "project":
                _cmd_project(rest)
                continue
            elif cmd == "connect":
                _cmd_connect(rest)
                continue
            elif cmd == "no-diff" or cmd == "nodiff":
                diff_loop_active = False
                _emit({"kind": "status", "text": "diff prompts disabled"})
                continue
            elif cmd == "context":
                _emit({"kind": "status", "text": epoch.baseline})
                continue
            else:
                # Try the full slash dispatch for hub-first commands
                from baird.agent_tools import ToolEnv
                from baird.slash import SlashContext, try_dispatch as _try_slash
                _emit_captured()
                slash_ctx = SlashContext(
                    hub=hub,
                    env=ToolEnv(hub=hub, project_id=config.project_id),
                    input_fn=_dialog_input_fn,
                    console=_capture_console,
                    active_host=None,
                    tool_registry=tool_registry,
                )
                slash_res = _try_slash(line[1:], slash_ctx)
                if slash_res is not None and slash_res.handled:
                    _emit_captured()
                    if slash_res.output:
                        _emit({"kind": "status", "text": slash_res.output})
                    if slash_res.next_user_prompt:
                        line = slash_res.next_user_prompt
                        # Suppress the full prompt — emit a brief status instead
                        brief = line.strip().splitlines()[0][:120]
                        _emit({"kind": "status", "text": brief + "…" if len(line) > 120 else brief})
                        _emit({"kind": "turn_start"})
                        seen_tool_names.clear()
                        _tool_result_count.clear()
                        try:
                            completion = _one_turn(
                                user_msg=line, hub=hub, model_client=model_client,
                                session_id=session["id"], config=config, system=system,
                                host_id=None, tool_registry=tool_registry,
                                agent_mode=agent_mode, on_chunk=_on_chunk,
                                on_tool_event=_on_tool_event,
                            )
                        except Exception as e:
                            _emit({"kind": "error", "text": str(e)})
                            continue
                        stats.turns += 1
                        stats.total_cost_usd += completion.cost_usd
                        stats.total_input_tokens += completion.usage.input_tokens
                        stats.total_output_tokens += completion.usage.output_tokens
                        _emit({"kind": "stream_end", "usage": {"inputTokens": completion.usage.input_tokens, "outputTokens": completion.usage.output_tokens, "costUsd": completion.cost_usd}})
                        _emit({"kind": "stats_update", "turns": stats.turns, "costUsd": stats.total_cost_usd, "inputTokens": stats.total_input_tokens, "outputTokens": stats.total_output_tokens})
                        continue
                else:
                    _emit_captured()
                    _emit({"kind": "error", "text": f"unknown command: /{cmd} (try /help)"})
                    continue

        _emit({"kind": "user_message", "text": line})
        _emit({"kind": "turn_start"})
        seen_tool_names.clear()
        _tool_result_count.clear()

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

        stats.turns += 1
        stats.total_cost_usd += completion.cost_usd
        stats.total_input_tokens += completion.usage.input_tokens
        stats.total_output_tokens += completion.usage.output_tokens

        _emit({"kind": "stream_end", "usage": {"inputTokens": completion.usage.input_tokens, "outputTokens": completion.usage.output_tokens, "costUsd": completion.cost_usd}})
        _emit({"kind": "stats_update", "turns": stats.turns, "costUsd": stats.total_cost_usd, "inputTokens": stats.total_input_tokens, "outputTokens": stats.total_output_tokens})

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
                    "body": f"Apply diff block? [{preview_text}]",
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
                        _emit({"kind": "status", "text": f"applied {result.commit_sha[:12]} ({len(result.files_changed)} file(s))"})
                    except DiffApplyError as e:
                        _emit({"kind": "error", "text": f"apply failed: {e}"})
                else:
                    _emit({"kind": "status", "text": "skipped"})

    _emit({"kind": "status", "text": f"session={session['id'][:8]}  turns={stats.turns}  total=${stats.total_cost_usd:.4f}"})


def _render_context(ctx) -> str:
    """Minimal context renderer (avoids Layout dependency)."""
    from baird.context_loader import render_context as _rc
    return _rc(ctx)


if __name__ == "__main__":
    try:
        _main()
    except Exception as e:
        import traceback
        sys.stderr.write(traceback.format_exc())
        sys.stdout.write(json.dumps({"kind": "error", "text": str(e)}) + "\n")
        sys.stdout.flush()
        sys.exit(1)
