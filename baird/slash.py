"""Slash-command dispatch for the BAIRD REPL.

A small registry of `/`-prefixed commands the user can run inside `baird code`.
Each command parses its own inline argv; missing required fields trigger the
slice-B `collect_form_values` helper, which prompts once at the end.

Commands shipped here are the new hub-first family — they wrap agent tools so
the user can register/edit projects, add locations, edit a satellite's
host.yaml, install an env, look up an alias, or run a one-off command on a
named satellite WITHOUT ssh'ing into the satellite.

REPL-native commands (/exit, /context, /reset, /model, /sessions, /project,
/no-diff) still live inline in repl.py + tui.py — they manipulate REPL state
those modules own. This module is for the "wrap a tool" family that doesn't
need REPL internals.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .agent_tools import (
    ApprovalGate,
    Tool,
    ToolEnv,
    build_catalogue,
    dispatch,
)
from .memory_client import HubClient
from .tui import FormField, collect_form_values


@dataclass
class SlashContext:
    hub: HubClient
    env: ToolEnv
    input_fn: Callable[[str], str]
    console: object | None = None  # rich Console; optional for tests
    active_host: str | None = None  # see slice E


@dataclass
class SlashResult:
    handled: bool
    output: str = ""
    ok: bool = True
    # When the command set/cleared an active host, the REPL picks it up.
    active_host: str | None = None
    clear_active_host: bool = False
    # Signal to the REPL: switch the active project to this id (re-loads
    # context, session, etc.). Set by /project new so create+switch is one step.
    switch_to_project: str | None = None


HandlerFn = Callable[[list[str], SlashContext], SlashResult]


# ---- argv parsing -----------------------------------------------------


def parse_kv_args(parts: Iterable[str]) -> tuple[list[str], dict[str, str]]:
    """Split `parts` into positional + key=value pairs. Quoted values OK."""
    positional: list[str] = []
    kv: dict[str, str] = {}
    for p in parts:
        if "=" in p and not p.startswith("="):
            k, _, v = p.partition("=")
            kv[k] = v
        else:
            positional.append(p)
    return positional, kv


def _absolute_path(v: str) -> str | None:
    return None if v.startswith("/") or v.startswith("~") else "must be an absolute path"


# ---- /project new -----------------------------------------------------


def _parse_locations_spec(spec: str) -> list[tuple[str, str]]:
    """Parse `host:path[,host:path...]` into a list of (host, path) pairs.

    Empty string → empty list. Whitespace around each comma-separated entry is
    trimmed. Entries without a colon, or with an empty host or path, are
    skipped silently — `collect_form_values` doesn't have a hook to re-prompt
    a single subfield, so we prefer succeed-with-warning over blocking project
    creation on a partial typo.
    """
    out: list[tuple[str, str]] = []
    for entry in (e.strip() for e in spec.split(",")):
        if not entry or ":" not in entry:
            continue
        host, _, path = entry.partition(":")
        host = host.strip()
        path = path.strip()
        if host and path:
            out.append((host, path))
    return out


def cmd_project_new(parts: list[str], ctx: SlashContext) -> SlashResult:
    pos, kv = parse_kv_args(parts)
    known: dict[str, str] = dict(kv)
    if pos:
        known.setdefault("id", pos[0])
        if len(pos) > 1:
            known.setdefault("name", " ".join(pos[1:]))
    fields = [
        FormField("id", "project id (slug)", required=True),
        FormField("name", "human-readable name", required=False),
        FormField("github", "GitHub repo (owner/name)", required=False),
        FormField(
            "locations",
            "locations (host:path[,host:path...]) — empty to add later",
            required=False,
        ),
    ]
    vals = collect_form_values(fields, known, input_fn=ctx.input_fn, console=ctx.console)
    result = ctx.hub.upsert_project(
        id=vals["id"],
        name=vals.get("name") or vals["id"],
        github=vals.get("github") or None,
    )
    pid = result["id"]
    added: list[tuple[str, str]] = []
    for host, path in _parse_locations_spec(vals.get("locations", "")):
        try:
            ctx.hub.add_project_location(pid, host=host, path=path, role=None)
            added.append((host, path))
        except Exception as e:  # surfaced to user; project row already exists
            return SlashResult(
                handled=True,
                ok=False,
                output=(
                    f"created project {pid}, but failed to add location "
                    f"{host}:{path}: {e}"
                ),
                switch_to_project=pid,
            )
    extra = f" with {len(added)} location(s)" if added else ""
    return SlashResult(
        handled=True,
        output=f"created project {pid}{extra}",
        switch_to_project=pid,
    )


# ---- /project locations ----------------------------------------------


def cmd_project_locations(parts: list[str], ctx: SlashContext) -> SlashResult:
    pos, kv = parse_kv_args(parts)
    known = dict(kv)
    if pos:
        known.setdefault("project_id", pos[0])
    fields = [FormField("project_id", "project id", required=True)]
    vals = collect_form_values(fields, known, input_fn=ctx.input_fn, console=ctx.console)
    rows = ctx.hub.list_project_locations(vals["project_id"])
    if not rows:
        return SlashResult(handled=True, output="(no locations)")
    body = "\n".join(
        f"  {r['host']:>12}  {r['path']}  [{r.get('role') or '-'}]" for r in rows
    )
    return SlashResult(handled=True, output=body)


# ---- /project add-location -------------------------------------------


def cmd_project_add_location(parts: list[str], ctx: SlashContext) -> SlashResult:
    pos, kv = parse_kv_args(parts)
    known = dict(kv)
    # Positional shorthand: /project add-location <pid> <host> <path> [role]
    if pos:
        keys = ["project_id", "host", "path", "role"]
        for k, v in zip(keys, pos, strict=False):
            known.setdefault(k, v)
    fields = [
        FormField("project_id", "project id", required=True),
        FormField("host", "host id", required=True),
        FormField("path", "path on the satellite", required=True, validator=_absolute_path),
        FormField("role", "role tag (data | compute | notebook | repo)", required=False),
    ]
    vals = collect_form_values(fields, known, input_fn=ctx.input_fn, console=ctx.console)
    rows = ctx.hub.add_project_location(
        vals["project_id"], host=vals["host"], path=vals["path"], role=vals.get("role")
    )
    return SlashResult(
        handled=True,
        output=f"location added — project {vals['project_id']} now has {len(rows)} location(s)",
    )


# ---- /host add (wraps satellite enroll) -------------------------------


def cmd_host_add(parts: list[str], ctx: SlashContext) -> SlashResult:
    """Form-style satellite enrolment. Delegates to satellite.enroll which
    does the SSH-out + bootstrap + tunnel install."""
    from .satellite import enroll, enroll_spec_from_local

    pos, kv = parse_kv_args(parts)
    known = dict(kv)
    if pos:
        known.setdefault("ssh_host", pos[0])
    fields = [
        FormField("ssh_host", "ssh alias or user@host", required=True),
        FormField("host_id", "BAIRD host_id (defaults to ssh alias)", required=False),
        FormField("git_ref", "BAIRD git ref to install", default="main", required=False),
        FormField("watch_root", "satellite watch root", default="~/projects", required=False),
    ]
    vals = collect_form_values(fields, known, input_fn=ctx.input_fn, console=ctx.console)
    spec = enroll_spec_from_local(
        vals["ssh_host"], host_id=vals.get("host_id") or None, git_ref=vals.get("git_ref", "main")
    )
    spec.remote_watch_root = vals.get("watch_root", "~/projects")
    res = enroll(spec)
    if not res.health_ok:
        return SlashResult(handled=True, ok=False, output=f"enrolment failed: {res.detail}")
    return SlashResult(
        handled=True,
        output=f"enrolled {res.ssh_host} (host_id={res.host_id}) port={res.local_fwd_port}",
    )


# ---- /host edit (edit host.yaml via executor) -------------------------


def cmd_host_edit(parts: list[str], ctx: SlashContext) -> SlashResult:
    """Wraps the `set_watch_root` agent tool — the only host.yaml field we
    currently expose for editing. Extending this to other host.yaml fields is
    a per-field tool call; the slice-B form pattern makes that cheap."""
    pos, kv = parse_kv_args(parts)
    known = dict(kv)
    if pos:
        if len(pos) >= 1:
            known.setdefault("host", pos[0])
        if len(pos) >= 2:
            known.setdefault("path", pos[1])
    if "host" not in known and ctx.active_host:
        known["host"] = ctx.active_host
    fields = [
        FormField("host", "satellite host_id", required=True),
        FormField("path", "new watch root path", required=True, validator=_absolute_path),
    ]
    vals = collect_form_values(fields, known, input_fn=ctx.input_fn, console=ctx.console)
    tool = build_catalogue()["set_watch_root"]
    return _run_tool(tool, vals, ctx)


# ---- /env install -----------------------------------------------------


def cmd_env_install(parts: list[str], ctx: SlashContext) -> SlashResult:
    pos, kv = parse_kv_args(parts)
    known = dict(kv)
    if pos:
        if len(pos) >= 1:
            known.setdefault("host", pos[0])
        if len(pos) >= 2:
            known.setdefault("project_id", pos[1])
    fields = [
        FormField("host", "satellite host_id", required=True),
        FormField("project_id", "project id", required=True),
        FormField(
            "env_spec",
            "pip requirements (one per line) OR environment.yml body",
            required=True,
        ),
    ]
    vals = collect_form_values(fields, known, input_fn=ctx.input_fn, console=ctx.console)
    tool = build_catalogue()["install_env"]
    # Tier-3 always — install_env is gated by an explicit user confirmation.
    confirm = ctx.input_fn(
        f"about to install env on {vals['host']} for {vals['project_id']} — confirm? [y/N]: "
    ).strip().lower()
    if confirm != "y":
        return SlashResult(handled=True, ok=False, output="cancelled")
    gate = ApprovalGate(auto_destructive=True)
    return _run_tool(tool, vals, ctx, gate=gate)


# ---- /where -----------------------------------------------------------


def cmd_where(parts: list[str], ctx: SlashContext) -> SlashResult:
    pos, kv = parse_kv_args(parts)
    known = dict(kv)
    if pos:
        known.setdefault("query", " ".join(pos))
    fields = [
        FormField("query", "alias name or path fragment", required=True),
    ]
    vals = collect_form_values(fields, known, input_fn=ctx.input_fn, console=ctx.console)
    tool = build_catalogue()["where"]
    return _run_tool(tool, vals, ctx)


# ---- /run on <host>: <cmd> -------------------------------------------


def cmd_run_on(parts: list[str], ctx: SlashContext) -> SlashResult:
    """Parses `on <host>: <command>` or `<host>: <command>`.

    The colon separator is required so commands can contain spaces/flags
    without quoting. Falls back to the form for missing pieces."""
    # Re-join then split on the first colon — the user typed
    # `/run on hibu: ls /data` and parts is ['on', 'hibu:', 'ls', '/data'].
    text = " ".join(parts)
    if text.lower().startswith("on "):
        text = text[3:]
    known: dict[str, str] = {}
    if ":" in text:
        head, _, cmd = text.partition(":")
        host = head.strip()
        command = cmd.strip()
        if host:
            known["host"] = host
        if command:
            known["command"] = command
    elif text:
        known["host"] = text.strip()
    # If no host inline but context has an active host, use it.
    if "host" not in known and ctx.active_host:
        known["host"] = ctx.active_host
    fields = [
        FormField("host", "satellite host_id", required=True),
        FormField("command", "command line", required=True),
    ]
    vals = collect_form_values(fields, known, input_fn=ctx.input_fn, console=ctx.console)
    tool = build_catalogue()["run_on"]
    res = _run_tool(tool, vals, ctx)
    # Whichever host the user picked, remember it for the rest of the turn.
    res.active_host = vals["host"]
    return res


# ---- tool runner -----------------------------------------------------


def _run_tool(
    tool: Tool,
    args: dict[str, str],
    ctx: SlashContext,
    *,
    gate: ApprovalGate | None = None,
) -> SlashResult:
    if gate is None:
        # Default for slash-driven calls: warn (don't block) on tier 2, prompt on tier 3.
        def _prompt_destructive(_msg: str) -> bool:
            ans = ctx.input_fn(
                f"{tool.name} is destructive ({_msg}) — proceed? [y/N]: "
            ).strip().lower()
            return ans == "y"

        # We can't easily express "prompt on tier 3" through ApprovalGate's
        # boolean alone, so we precheck via classify_tool_call and re-dispatch
        # with auto_destructive=True only after the user assents.
        from .agent_tools import classify_tool_call

        decision = classify_tool_call(tool, args)
        if decision.tier.value == "destructive":
            if not _prompt_destructive(decision.reason):
                return SlashResult(handled=True, ok=False, output="cancelled")
            gate = ApprovalGate(auto_destructive=True)
        else:
            gate = ApprovalGate()
    result = dispatch(tool, dict(args), ctx.env, gate=gate)
    if not result.ok:
        return SlashResult(handled=True, ok=False, output=f"error: {result.error}")
    return SlashResult(handled=True, output=_format_tool_result(result.result))


def _format_tool_result(result) -> str:
    if result is None:
        return "ok"
    if isinstance(result, list):
        if not result:
            return "(empty)"
        return "\n".join(str(r) for r in result)
    if isinstance(result, dict):
        return "\n".join(f"  {k}: {v}" for k, v in result.items())
    return str(result)


# ---- Registry --------------------------------------------------------


_COMMANDS: dict[str, HandlerFn] = {
    "project new": cmd_project_new,
    "project locations": cmd_project_locations,
    "project add-location": cmd_project_add_location,
    "host add": cmd_host_add,
    "host edit": cmd_host_edit,
    "env install": cmd_env_install,
    "where": cmd_where,
    "run": cmd_run_on,
}


def commands() -> list[str]:
    """Return the registered slash-command verb-strings, for /help."""
    return sorted(_COMMANDS.keys())


def try_dispatch(line: str, ctx: SlashContext) -> SlashResult | None:
    """If `line` (without the leading `/`) matches a registered command,
    dispatch it; otherwise return None so the caller's own slash registry
    can take a turn."""
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    if not tokens:
        return None
    # Try two-word verbs first, then one-word.
    for verb_len in (2, 1):
        if len(tokens) < verb_len:
            continue
        verb = " ".join(tokens[:verb_len])
        if verb in _COMMANDS:
            return _COMMANDS[verb](tokens[verb_len:], ctx)
    return None


__all__ = [
    "SlashContext",
    "SlashResult",
    "commands",
    "try_dispatch",
    "parse_kv_args",
]
