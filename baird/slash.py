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
from .project_enrich import (
    EnrichmentProposal,
    LocationProbe,
    probe_location,
    propose_enrichment,
)
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
    """Split `parts` into positional + key=value pairs. Quoted values OK.

    Kept as a thin wrapper over `parse_inline_args` for back-compat. New
    code should prefer `parse_inline_args` so it can surface parse errors.
    """
    pos, kv, _err = parse_inline_args(list(parts))
    return pos, kv


def parse_inline_args(
    parts: list[str],
) -> tuple[list[str], dict[str, str], str | None]:
    """Parse a slash command's token list into (positional, flags, error).

    Recognised shapes:
      - `--key value`  — two tokens
      - `--key=value`  — single token
      - `key=value`    — single token
      - anything else  — positional

    A flag whose value starts with `--` (or that has no value) is rejected
    with a clear error — see issue #2 — because in real use that always
    means an upstream flag was mistyped (e.g. `/foo --bar --baz qux` where
    the user meant `/foo --bar X --baz qux`).
    """
    positional: list[str] = []
    kv: dict[str, str] = {}
    i = 0
    while i < len(parts):
        p = parts[i]
        if p.startswith("--") and len(p) > 2:
            key = p[2:]
            if "=" in key:
                k, _, v = key.partition("=")
                if not k:
                    return positional, kv, f"malformed flag {p!r}"
                if v.startswith("--"):
                    return (
                        positional,
                        kv,
                        f"flag --{k} has a flag-looking value {v!r} — "
                        f"did you mean to quote it?",
                    )
                kv[k] = v
                i += 1
                continue
            if i + 1 >= len(parts):
                return positional, kv, f"flag --{key} is missing a value"
            v = parts[i + 1]
            if v.startswith("--"):
                return (
                    positional,
                    kv,
                    f"flag --{key} has a flag-looking value {v!r} — "
                    f"did you mean to quote it, or supply a value for --{key}?",
                )
            kv[key] = v
            i += 2
            continue
        if "=" in p and not p.startswith("="):
            k, _, v = p.partition("=")
            kv[k] = v
            i += 1
            continue
        positional.append(p)
        i += 1
    return positional, kv, None


def _reject_flaglike_values(known: dict[str, str]) -> str | None:
    """Defensive guard for issue #2: refuse to submit a form when any
    already-supplied value starts with `--`. The inline parser catches
    this at parse time, but if a flag-looking string ever sneaks into a
    positional slot via shell quoting or a regression, this stops the
    form from happily storing it. Returns a clear error message or None.
    """
    for k, v in known.items():
        if isinstance(v, str) and v.startswith("--"):
            hint = v[2:].split()[0] if len(v) > 2 else ""
            suffix = f" — did you mean `--{hint} <value>`?" if hint else ""
            return (
                f"value for {k!r} starts with '--' ({v!r}) — "
                f"looks like an unparsed flag{suffix}"
            )
    return None


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
    # Generic parser handles `--<field> <value>` and `--<field>=<value>`
    # for ANY field, not just --parent. See issue #1.
    pos, kv, err = parse_inline_args(parts)
    if err:
        return SlashResult(handled=True, ok=False, output=err)
    known: dict[str, str] = dict(kv)
    if pos:
        known.setdefault("id", pos[0])
        if len(pos) > 1:
            known.setdefault("name", " ".join(pos[1:]))
    guard = _reject_flaglike_values(known)
    if guard:
        return SlashResult(handled=True, ok=False, output=guard)
    fields = [
        FormField("id", "project id (slug)", required=True),
        FormField("name", "human-readable name", required=False),
        FormField("github", "GitHub repo (owner/name)", required=False),
        FormField(
            "parent",
            "parent project id (empty for top-level)",
            required=False,
        ),
        FormField(
            "locations",
            "locations (host:path[,host:path...]) — empty to add later",
            required=False,
        ),
    ]
    vals = collect_form_values(fields, known, input_fn=ctx.input_fn, console=ctx.console)
    parent_id: str | None = (vals.get("parent") or "").strip() or None
    if parent_id is not None:
        canonical_parent, perr = _resolve_parent_project(parent_id, ctx)
        if canonical_parent is None:
            return SlashResult(handled=True, ok=False, output=perr or "unknown parent")
        parent_id = canonical_parent
    try:
        result = ctx.hub.upsert_project(
            id=vals["id"],
            name=vals.get("name") or vals["id"],
            github=vals.get("github") or None,
            parent_id=parent_id,
        )
    except Exception as e:
        return SlashResult(
            handled=True, ok=False, output=f"failed to create project: {e}"
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
    enrich_note = ""
    if added:
        # Only auto-enrich when at least one location was attached — there's
        # nothing to probe otherwise. The user can re-run later with
        # `/project enrich <id>` once locations are added.
        try:
            enrich_summary = _run_enrichment(pid, ctx)
            if enrich_summary:
                enrich_note = f"\n{enrich_summary}"
        except Exception as e:  # never block creation on enrichment failure
            enrich_note = f"\n(enrichment skipped: {e})"
    return SlashResult(
        handled=True,
        output=f"created project {pid}{extra}{enrich_note}",
        switch_to_project=pid,
    )


# ---- parent-id validation --------------------------------------------


def _resolve_parent_project(
    parent_id: str, ctx: SlashContext
) -> tuple[str | None, str | None]:
    """Validate `parent_id` against the hub's project list and suggest the
    closest match on miss (mirrors `_resolve_satellite_host` for hosts).

    Returns `(canonical_id, None)` on success or `(None, error)` on miss.
    """
    import difflib

    try:
        projects = ctx.hub.list_projects()
    except Exception as e:
        return None, f"could not list projects to validate parent: {e}"
    by_id = {p["id"]: p for p in projects}
    if parent_id in by_id:
        return parent_id, None
    by_lower = {pid.lower(): pid for pid in by_id}
    canonical = by_lower.get(parent_id.lower())
    if canonical is not None:
        return canonical, None
    suggestions = difflib.get_close_matches(
        parent_id.lower(), list(by_lower), n=1, cutoff=0.4
    )
    suggestion = (
        f" did you mean `{by_lower[suggestions[0]]}`?" if suggestions else ""
    )
    known_ids = ", ".join(sorted(by_id)) or "(none)"
    return None, (
        f"unknown parent project {parent_id!r}.{suggestion} "
        f"Known projects: {known_ids}."
    )


# ---- /project enrich --------------------------------------------------


def cmd_project_enrich(parts: list[str], ctx: SlashContext) -> SlashResult:
    pos, kv, err = parse_inline_args(parts)
    if err:
        return SlashResult(handled=True, ok=False, output=err)
    known = dict(kv)
    if pos:
        known.setdefault("project_id", pos[0])
    guard = _reject_flaglike_values(known)
    if guard:
        return SlashResult(handled=True, ok=False, output=guard)
    fields = [FormField("project_id", "project id", required=True)]
    vals = collect_form_values(fields, known, input_fn=ctx.input_fn, console=ctx.console)
    try:
        summary = _run_enrichment(vals["project_id"], ctx)
    except Exception as e:
        return SlashResult(handled=True, ok=False, output=f"enrichment failed: {e}")
    return SlashResult(handled=True, output=summary or "no enrichment proposals")


def _make_reader_from_env(env: ToolEnv):
    """Build a `RemoteReader` over the ToolEnv's executor factory. Returns
    None for missing files; raises only for hard transport errors so the
    probe layer can short-circuit them per file."""

    def _read(host: str, path: str) -> str | None:
        try:
            with env.open_executor(host) as ex:
                body = ex.read_file(path)
            return body.get("content") if isinstance(body, dict) else None
        except Exception:
            # Most read_file misses come back as HTTPStatusError(404) or
            # FileNotFoundError; treat them all as "not present" so the
            # probe just leaves the slot empty.
            return None

    return _read


def _run_enrichment(project_id: str, ctx: SlashContext) -> str:
    """Probe each location of `project_id`, present field proposals via
    `collect_form_values`, and save accepted values back through
    `hub.upsert_project`. Returns a short summary string for the REPL."""
    project = ctx.hub.get_project(project_id)
    locations = ctx.hub.list_project_locations(project_id)
    if not locations:
        return "no locations to probe — add one with /project add-location"

    reader = _make_reader_from_env(ctx.env)
    probes: list[LocationProbe] = [
        probe_location(reader, loc["host"], loc["path"]) for loc in locations
    ]
    proposal: EnrichmentProposal = propose_enrichment(project, probes)
    if not proposal.proposals:
        return "no empty fields to enrich"

    # Build a form: one field per proposal. The user gets the proposed
    # value as the default, "(none found — leave blank?)" for misses, and
    # can accept (enter), edit (type), or blank (type a single dash).
    form_fields: list[FormField] = []
    proposed_serialized: dict[str, str] = {}
    for prop in proposal.proposals:
        if prop.value is None:
            label = f"{prop.field} — (none found — leave blank?) [source: {prop.source}]"
            default = ""
        else:
            display = (
                str(prop.value)
                if not isinstance(prop.value, dict)
                else ", ".join(f"{k}={v}" for k, v in prop.value.items())
            )
            label = f"{prop.field} — accept [source: {prop.source}]"
            default = display
            proposed_serialized[prop.field] = display
        form_fields.append(FormField(prop.field, label, default=default or None, required=False))

    # We pass an empty `known` so each field is presented; users hit enter
    # to accept the default (the proposed value), type something else to
    # edit, or type "-" to blank.
    answers = collect_form_values(
        form_fields, {}, input_fn=ctx.input_fn, console=ctx.console
    )

    # Translate answers back into upsert_project arguments. `-` means blank
    # (user explicitly rejected the proposal); empty string means "keep
    # existing" — but since we only proposed for empty fields, both are
    # effectively the same for now.
    updates: dict[str, object] = {}
    cfg_updates: dict[str, object] = {}
    for prop in proposal.proposals:
        raw = answers.get(prop.field, "")
        if raw == "-" or raw == "":
            continue
        if prop.field == "env":
            # The user may have edited a dict-as-text — parse `k=v,k=v`.
            cfg_updates["env"] = _parse_kv_dict(raw, fallback=prop.value)
        else:
            updates[prop.field] = raw

    if not updates and not cfg_updates:
        return "no changes accepted from enrichment proposals"

    new_cfg = dict(project.get("config") or {})
    new_cfg.update(cfg_updates)
    ctx.hub.upsert_project(
        id=project["id"],
        name=project.get("name") or project["id"],
        github=updates.get("github", project.get("github")),
        context=updates.get("context", project.get("context")),
        config=new_cfg,
    )
    accepted = list(updates) + list(cfg_updates)
    return f"enriched: {', '.join(accepted)}"


def _parse_kv_dict(text: str, fallback: object | None) -> dict:
    """Parse `k=v,k=v` back into a dict, falling back to the original
    proposal when parsing fails (user edited freeform). The form layer
    doesn't have a way to round-trip a typed object, so dicts go through
    a string representation."""
    if isinstance(fallback, dict):
        out = dict(fallback)
    else:
        out = {}
    for entry in (e.strip() for e in text.split(",")):
        if not entry or "=" not in entry:
            continue
        k, _, v = entry.partition("=")
        out[k.strip()] = v.strip()
    return out


# ---- /project locations ----------------------------------------------


def cmd_project_locations(parts: list[str], ctx: SlashContext) -> SlashResult:
    pos, kv, err = parse_inline_args(parts)
    if err:
        return SlashResult(handled=True, ok=False, output=err)
    known = dict(kv)
    if pos:
        known.setdefault("project_id", pos[0])
    guard = _reject_flaglike_values(known)
    if guard:
        return SlashResult(handled=True, ok=False, output=guard)
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


def _resolve_satellite_host(host: str) -> tuple[str | None, str | None]:
    """Match `host` against the satellite registry.

    Returns `(canonical_host_id, None)` on a hit (case-insensitive match;
    the stored value is the registry's casing). Returns `(None, error)`
    when there's no match — the error message lists enrolled hosts and a
    closest-match suggestion via difflib, so the user can correct the
    typo without having to run `baird satellite list` themselves.

    Lives in slash.py because the validation is a UX concern (turning a
    bare ValueError from the hub into actionable text); other call sites
    (e.g. add_project_location agent tool) can either re-use this or let
    the hub raise raw.
    """
    import difflib

    from .satellite import load_registry

    try:
        reg = load_registry()
    except Exception as e:
        # Fail open — better to let the hub call surface its own error than
        # to block legitimate additions because the registry is unreadable.
        return host, f"(warning: could not read satellite registry: {e})"
    if not reg:
        return None, "no satellites enrolled — run `/host add <ssh_host>` first"
    by_lower = {hid.lower(): hid for hid in reg}
    canonical = by_lower.get(host.lower())
    if canonical is not None:
        return canonical, None
    suggestions = difflib.get_close_matches(host.lower(), list(by_lower), n=1, cutoff=0.4)
    suggestion = (
        f" did you mean `{by_lower[suggestions[0]]}`?" if suggestions else ""
    )
    enrolled = ", ".join(sorted(reg))
    return None, (
        f"unknown host {host!r}.{suggestion} "
        f"Enrolled satellites: {enrolled}."
    )


def cmd_project_add_location(parts: list[str], ctx: SlashContext) -> SlashResult:
    pos, kv, err = parse_inline_args(parts)
    if err:
        return SlashResult(handled=True, ok=False, output=err)
    known = dict(kv)
    # Positional shorthand: /project add-location <pid> <host> <path> [role]
    if pos:
        keys = ["project_id", "host", "path", "role"]
        for k, v in zip(keys, pos, strict=False):
            known.setdefault(k, v)
    guard = _reject_flaglike_values(known)
    if guard:
        return SlashResult(handled=True, ok=False, output=guard)
    fields = [
        FormField("project_id", "project id", required=True),
        FormField("host", "host id", required=True),
        FormField("path", "path on the satellite", required=True, validator=_absolute_path),
        FormField("role", "role tag (data | compute | notebook | repo)", required=False),
    ]
    vals = collect_form_values(fields, known, input_fn=ctx.input_fn, console=ctx.console)
    canonical, err = _resolve_satellite_host(vals["host"])
    if canonical is None:
        return SlashResult(handled=True, ok=False, output=err or "unknown host")
    vals["host"] = canonical
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

    pos, kv, err = parse_inline_args(parts)
    if err:
        return SlashResult(handled=True, ok=False, output=err)
    known = dict(kv)
    if pos:
        known.setdefault("ssh_host", pos[0])
    guard = _reject_flaglike_values(known)
    if guard:
        return SlashResult(handled=True, ok=False, output=guard)
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
    pos, kv, err = parse_inline_args(parts)
    if err:
        return SlashResult(handled=True, ok=False, output=err)
    known = dict(kv)
    if pos:
        if len(pos) >= 1:
            known.setdefault("host", pos[0])
        if len(pos) >= 2:
            known.setdefault("path", pos[1])
    if "host" not in known and ctx.active_host:
        known["host"] = ctx.active_host
    guard = _reject_flaglike_values(known)
    if guard:
        return SlashResult(handled=True, ok=False, output=guard)
    fields = [
        FormField("host", "satellite host_id", required=True),
        FormField("path", "new watch root path", required=True, validator=_absolute_path),
    ]
    vals = collect_form_values(fields, known, input_fn=ctx.input_fn, console=ctx.console)
    tool = build_catalogue()["set_watch_root"]
    return _run_tool(tool, vals, ctx)


# ---- /env install -----------------------------------------------------


def cmd_env_install(parts: list[str], ctx: SlashContext) -> SlashResult:
    pos, kv, err = parse_inline_args(parts)
    if err:
        return SlashResult(handled=True, ok=False, output=err)
    known = dict(kv)
    if pos:
        if len(pos) >= 1:
            known.setdefault("host", pos[0])
        if len(pos) >= 2:
            known.setdefault("project_id", pos[1])
    guard = _reject_flaglike_values(known)
    if guard:
        return SlashResult(handled=True, ok=False, output=guard)
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
    pos, kv, err = parse_inline_args(parts)
    if err:
        return SlashResult(handled=True, ok=False, output=err)
    known = dict(kv)
    if pos:
        known.setdefault("query", " ".join(pos))
    guard = _reject_flaglike_values(known)
    if guard:
        return SlashResult(handled=True, ok=False, output=guard)
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


# ---- /project tree ---------------------------------------------------


def cmd_project_tree(parts: list[str], ctx: SlashContext) -> SlashResult:
    """Render the project hierarchy as an indented tree.

    Rules:
      - Roots = projects with no parent_id.
      - Roots with at least one child render as `<id>/` followed by indented
        children (umbrellas).
      - Roots with no children render as standalone leaves.
      - Children always indent two spaces under their parent.
    """
    try:
        projects = ctx.hub.list_projects()
    except Exception as e:
        return SlashResult(handled=True, ok=False, output=f"failed to list projects: {e}")
    if not projects:
        return SlashResult(handled=True, output="(no projects)")

    def _parent(p: dict) -> str | None:
        if p.get("parent_id"):
            return p["parent_id"]
        return (p.get("config") or {}).get("parent_id")

    children_of: dict[str, list[dict]] = {}
    for p in projects:
        pid = _parent(p)
        if pid is not None:
            children_of.setdefault(pid, []).append(p)
    roots = sorted(
        (p for p in projects if _parent(p) is None), key=lambda p: p["id"]
    )

    lines: list[str] = []
    for r in roots:
        kids = sorted(children_of.get(r["id"], []), key=lambda c: c["id"])
        if kids:
            lines.append(f"{r['id']}/  — {r.get('name') or r['id']}")
            for k in kids:
                lines.append(f"  {k['id']}  — {k.get('name') or k['id']}")
        else:
            lines.append(f"{r['id']}  — {r.get('name') or r['id']}")
    return SlashResult(handled=True, output="\n".join(lines))


# ---- /project siblings -----------------------------------------------


def cmd_project_siblings(parts: list[str], ctx: SlashContext) -> SlashResult:
    """List sibling project ids — projects sharing the same parent as the
    active (or named) project. Helpful for the agent to discover what else
    is under the same research programme. No-op message when the project
    is top-level."""
    pos, kv, err = parse_inline_args(parts)
    if err:
        return SlashResult(handled=True, ok=False, output=err)
    known = dict(kv)
    if pos:
        known.setdefault("project_id", pos[0])
    guard = _reject_flaglike_values(known)
    if guard:
        return SlashResult(handled=True, ok=False, output=guard)
    project_id = known.get("project_id") or ctx.env.project_id
    if not project_id:
        return SlashResult(
            handled=True,
            ok=False,
            output="no active project — pass an id: /project siblings <pid>",
        )
    try:
        me = ctx.hub.get_project(project_id)
    except Exception as e:
        return SlashResult(handled=True, ok=False, output=f"failed to load project: {e}")
    parent_id = me.get("parent_id") or (me.get("config") or {}).get("parent_id")
    if not parent_id:
        return SlashResult(
            handled=True,
            output=f"{project_id} has no parent (top-level project) — no siblings.",
        )
    try:
        kids = ctx.hub.list_children(parent_id)
    except Exception as e:
        return SlashResult(handled=True, ok=False, output=f"failed to list siblings: {e}")
    others = [k for k in kids if k["id"] != project_id]
    if not others:
        return SlashResult(
            handled=True, output=f"(no siblings under {parent_id})"
        )
    lines = [f"siblings under {parent_id}:"] + [
        f"  {k['id']}  — {k.get('name') or k['id']}" for k in sorted(others, key=lambda x: x["id"])
    ]
    return SlashResult(handled=True, output="\n".join(lines))


# ---- /project rename -------------------------------------------------


def cmd_project_rename(parts: list[str], ctx: SlashContext) -> SlashResult:
    """Rename a project's display name. Issue #3.

    Inline shape: `/project rename <id> <new name with spaces>`. The id
    is the first token; everything after it is treated as the new name
    (joined with a single space) so the user doesn't have to quote names
    that contain spaces. Empty form (`/project rename`) prompts for both.
    """
    # We do NOT call parse_inline_args here — names are free text that can
    # legitimately contain `=`, double dashes (e.g. "scRNA -- spatial"), or
    # `key=value` lookalikes. The id is always pos[0]; the name is the rest.
    known: dict[str, str] = {}
    if parts:
        known["id"] = parts[0]
    if len(parts) > 1:
        known["name"] = " ".join(parts[1:])
    fields = [
        FormField("id", "project id to rename", required=True),
        FormField("name", "new human-readable name", required=True),
    ]
    vals = collect_form_values(fields, known, input_fn=ctx.input_fn, console=ctx.console)
    new_name = vals["name"].strip()
    if not new_name:
        return SlashResult(handled=True, ok=False, output="new name cannot be empty")
    try:
        row = ctx.hub.rename_project(vals["id"], new_name)
    except Exception as e:
        return SlashResult(
            handled=True, ok=False, output=f"failed to rename project: {e}"
        )
    return SlashResult(
        handled=True,
        output=f"renamed {row['id']} → {row.get('name', new_name)!r}",
    )


# ---- /project delete -------------------------------------------------


def cmd_project_delete(parts: list[str], ctx: SlashContext) -> SlashResult:
    """Delete a project. Tier 3 (destructive) — always prompts y/N showing
    id, name, and any child count before going through. Hub rejects if
    children exist; we still show the child count up-front so the user
    isn't surprised by the server-side error."""
    known: dict[str, str] = {}
    if parts:
        known["id"] = parts[0]
    fields = [FormField("id", "project id to delete", required=True)]
    vals = collect_form_values(fields, known, input_fn=ctx.input_fn, console=ctx.console)
    pid = vals["id"]
    try:
        proj = ctx.hub.get_project(pid)
    except Exception as e:
        return SlashResult(handled=True, ok=False, output=f"project not found: {e}")
    try:
        kids = ctx.hub.list_children(pid)
    except Exception:
        kids = []
    name = proj.get("name") or pid
    if kids:
        kid_ids = ", ".join(sorted(k["id"] for k in kids))
        return SlashResult(
            handled=True,
            ok=False,
            output=(
                f"refusing to delete {pid!r} ({name}): has {len(kids)} child "
                f"project(s) ({kid_ids}). Reparent or delete them first."
            ),
        )
    prompt = f"delete project {pid!r} ({name})? [y/N] "
    answer = (ctx.input_fn(prompt) or "").strip().lower()
    if answer not in ("y", "yes"):
        return SlashResult(handled=True, output="aborted")
    try:
        ctx.hub.delete_project(pid)
    except Exception as e:
        return SlashResult(handled=True, ok=False, output=f"failed to delete: {e}")
    # If the deleted project happens to be the REPL's current active one,
    # the header will be stale until the user `/project new ...`s or
    # restarts. Worth a follow-up but not load-bearing for the bug fix.
    return SlashResult(handled=True, output=f"deleted project {pid}")


# ---- Registry --------------------------------------------------------


_COMMANDS: dict[str, HandlerFn] = {
    "project new": cmd_project_new,
    "project rename": cmd_project_rename,
    "project delete": cmd_project_delete,
    "project locations": cmd_project_locations,
    "project add-location": cmd_project_add_location,
    "project enrich": cmd_project_enrich,
    "project tree": cmd_project_tree,
    "project siblings": cmd_project_siblings,
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
    from .tui import FormParseError

    for verb_len in (2, 1):
        if len(tokens) < verb_len:
            continue
        verb = " ".join(tokens[:verb_len])
        if verb in _COMMANDS:
            try:
                return _COMMANDS[verb](tokens[verb_len:], ctx)
            except FormParseError as e:
                # Issue #2 fallback: a flag-looking value reached the form.
                return SlashResult(handled=True, ok=False, output=str(e))
    return None


__all__ = [
    "SlashContext",
    "SlashResult",
    "commands",
    "try_dispatch",
    "parse_kv_args",
]
