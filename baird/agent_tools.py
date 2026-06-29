"""Agent tool catalogue — the operations the user can invoke from the hub
without ssh'ing into a satellite.

Each tool has:
  - a JSON-schema descriptor (for surfacing to a model/LLM as a tool spec)
  - a Python callable that takes typed arguments + a `ToolEnv`
  - a tier classification (safe / project / destructive) so the dispatcher
    can route auto-runs vs. prompt-the-user vs. always-prompt

Two families:
  - Remote substrate: `read_remote`, `write_remote`, `run_on`, `apply_diff_remote`
    — wraps the satellite executor with an explicit `host` parameter.
  - Project management: `register_project`, `add_project_location`,
    `list_project_locations`, `set_watch_root`, `install_env`, `where`.

DESIGN NOTE — host parameter shape: we picked "explicit host param on a single
tool" over "one tool per host". The agent gets a small, stable surface
(`run_on(host, command, ...)`) instead of N exploded variants, and the
dispatcher resolves the host to an ExecutorClient via the satellite registry.
The Slice E "active host" heuristic in the REPL fills in `host` automatically
when the user has already named one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .memory_client import HubClient
from .permissions import Decision, Tier, classify_command, classify_write
from .satellite import load_registry

# ---- Environment passed to every tool ---------------------------------


@dataclass
class ToolEnv:
    """What a tool needs to do its job. Built once per REPL turn."""

    hub: HubClient
    project_id: str | None = None
    # Map of host_id → (base_url, auth_token). The hub-side dispatcher fills
    # this from `satellites.json`. None means "look it up on demand".
    executors: dict[str, tuple[str, str]] | None = None
    # Optional override; tests pass a custom executor factory.
    executor_factory: Callable[[str, str], Any] | None = None

    def open_executor(self, host: str):
        from .executor_client import ExecutorClient

        if self.executor_factory is not None:
            base_url, token = self._resolve(host)
            return self.executor_factory(base_url, token)
        base_url, token = self._resolve(host)
        return ExecutorClient(base_url, token)

    def _resolve(self, host: str) -> tuple[str, str]:
        if self.executors is not None and host in self.executors:
            return self.executors[host]
        reg = load_registry()
        entry = reg.get(host)
        if entry is None:
            raise ToolError(f"no satellite enrolled as {host!r}")
        port = entry["local_fwd_port"]
        token = entry["executor_auth_token"]
        return f"http://127.0.0.1:{port}", token


class ToolError(Exception):
    """Raised when a tool call can't be executed — bad args, missing host, etc.
    The dispatcher converts these to clean user-facing messages."""


# ---- Tool spec --------------------------------------------------------


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON-schema (object) for the args
    tier: Tier
    fn: Callable[..., Any]  # takes (env: ToolEnv, **args)
    needs_command_classification: bool = False
    """When True, the dispatcher reclassifies based on the `command` arg via
    `permissions.classify_command` (so `run_on` can elevate to tier-3 for any
    matching always-destructive command without us hard-coding it)."""


# ---- Remote substrate -------------------------------------------------


def _read_remote(env: ToolEnv, *, host: str, path: str) -> dict:
    with env.open_executor(host) as ex:
        return ex.read_file(path)


def _write_remote(
    env: ToolEnv, *, host: str, path: str, content: str, project_root: str | None = None
) -> dict:
    with env.open_executor(host) as ex:
        return ex.write_file(path, content, project_root=project_root)


def _run_on(
    env: ToolEnv,
    *,
    host: str,
    command: str,
    cwd: str | None = None,
    project_root: str | None = None,
    timeout_s: float = 30.0,
) -> dict:
    with env.open_executor(host) as ex:
        return ex.run_command(
            command, cwd=cwd, project_root=project_root, timeout_s=timeout_s
        )


def _apply_diff_remote(
    env: ToolEnv,
    *,
    host: str,
    project_root: str,
    diff: str,
    commit_message: str,
) -> dict:
    with env.open_executor(host) as ex:
        return ex.apply_diff(
            diff, project_root=project_root, commit_message=commit_message
        )


# ---- Project management -----------------------------------------------


def _register_project(env: ToolEnv, *, id: str, name: str | None = None) -> dict:
    return env.hub.upsert_project(id=id, name=name or id)


def _add_project_location(
    env: ToolEnv, *, project_id: str, host: str, path: str, role: str | None = None
) -> list[dict]:
    return env.hub.add_project_location(project_id, host=host, path=path, role=role)


def _list_projects(env: ToolEnv) -> list[dict]:
    return env.hub.list_projects()


def _list_project_locations(env: ToolEnv, *, project_id: str) -> list[dict]:
    return env.hub.list_project_locations(project_id)


def _set_watch_root(env: ToolEnv, *, host: str, path: str) -> dict:
    """Edit the satellite's host.yaml `watch.roots` to point at `path` and
    restart its baird-daemon user unit.

    Done as read_file → edit on the hub → write_file → systemctl restart, so
    the hub is the only place the user types and we don't need a shell heredoc
    on the satellite.

    NB: `~/.baird/host.yaml` must be inside one of the satellite's mounted
    volumes for the executor to allow the read/write; the default volume map
    includes the user's home so this works out of the box."""
    import yaml as _yaml

    yaml_path = "~/.baird/host.yaml"
    with env.open_executor(host) as ex:
        body = ex.read_file(yaml_path)
        data = _yaml.safe_load(body.get("content", "")) or {}
        data.setdefault("watch", {})["roots"] = [path]
        new_body = _yaml.safe_dump(data, sort_keys=False)
        write = ex.write_file(yaml_path, new_body, project_root=body.get("path"))
        # Restart is best-effort — the user-unit may not be installed on every
        # satellite. If it isn't, watch.roots is still updated for next start.
        restart = ex.run_command(
            "systemctl --user restart baird-daemon", timeout_s=15
        )
    return {"write": write, "restart": restart}


def _install_env(
    env: ToolEnv, *, host: str, project_id: str, env_spec: str
) -> dict:
    """Install an environment spec on the satellite. Tier-3 — the dispatcher
    will always prompt before running this, since pip/conda installs mutate
    system state.

    `env_spec` is interpreted as: an `environment.yml` body (conda) if it
    contains a `name:` or `dependencies:` line, otherwise a list of pip
    requirements (one per line)."""
    is_conda = "dependencies:" in env_spec or env_spec.lstrip().startswith("name:")
    if is_conda:
        write = {"path": "/tmp/baird-env.yml", "content": env_spec}
        cmd = (
            "conda env update --file /tmp/baird-env.yml --prune "
            "|| mamba env update --file /tmp/baird-env.yml --prune"
        )
    else:
        write = {"path": "/tmp/baird-reqs.txt", "content": env_spec}
        cmd = "pip install -r /tmp/baird-reqs.txt"
    with env.open_executor(host) as ex:
        ex.write_file(write["path"], write["content"], project_root="/tmp")
        out = ex.run_command(cmd, timeout_s=600)
    return {"project_id": project_id, "command": cmd, "result": out}


def _family_projects(env: ToolEnv, project_id: str) -> list[dict]:
    """Return all projects in this project's "family": the project itself,
    its parent (if any), and its siblings (other children of the same
    parent). For a top-level project with no children, returns `[self]`.
    For an umbrella project, returns `[self, ...children]`.

    Used by `where` so the agent can find files across the whole research
    programme when working inside one assay — the umbrella programme use case.
    """
    try:
        me = env.hub.get_project(project_id)
    except Exception:
        return []
    parent_id = me.get("parent_id") or (me.get("config") or {}).get("parent_id")
    family: list[dict] = [me]
    if parent_id:
        try:
            family.append(env.hub.get_project(parent_id))
        except Exception:
            pass
        try:
            for sib in env.hub.list_children(parent_id):
                if sib["id"] != project_id:
                    family.append(sib)
        except Exception:
            pass
    else:
        try:
            family.extend(env.hub.list_children(project_id))
        except Exception:
            pass
    return family


def _where(env: ToolEnv, *, query: str, project_id: str | None = None) -> list[dict]:
    """Resolve a data alias or partial path to concrete (host, path) pairs by
    looking across project locations + data_aliases. Free-text matched.

    Family-aware: when the project is part of a parent/child hierarchy, the
    search expands to include the parent and all sibling projects, so the
    agent can find data from any assay while working in one. The `project_id`
    field on each hit identifies the family member it came from.
    """
    pid = project_id or env.project_id
    if pid is None:
        return []
    family = _family_projects(env, pid)
    if not family:
        return []

    needle = query.lower()
    hits: list[dict] = []
    for proj in family:
        fid = proj["id"]
        try:
            locs = env.hub.list_project_locations(fid)
        except Exception:
            locs = []
        cfg = proj.get("config") or {}
        aliases = cfg.get("data_aliases") or []

        for a in aliases:
            if needle in (a.get("name", "") + a.get("path", "")).lower():
                hits.append({
                    "kind": "alias",
                    "project_id": fid,
                    "name": a.get("name"),
                    "host": a.get("volume", "").split(":")[0],
                    "path": a.get("path"),
                })
        for loc in locs:
            if needle in (loc.get("path", "") + (loc.get("role") or "")).lower():
                hits.append({
                    "kind": "location",
                    "project_id": fid,
                    "host": loc.get("host"),
                    "path": loc.get("path"),
                    "role": loc.get("role"),
                })
    return hits


def _list_siblings(env: ToolEnv, *, project_id: str | None = None) -> list[dict]:
    """Return sibling projects of `project_id` — other children of the same
    parent. Empty list when the project is top-level. Self is excluded."""
    pid = project_id or env.project_id
    if not pid:
        return []
    try:
        me = env.hub.get_project(pid)
    except Exception:
        return []
    parent_id = me.get("parent_id") or (me.get("config") or {}).get("parent_id")
    if not parent_id:
        return []
    try:
        kids = env.hub.list_children(parent_id)
    except Exception:
        return []
    return [
        {"id": k["id"], "name": k.get("name") or k["id"]}
        for k in kids
        if k["id"] != pid
    ]


# ---- Catalogue --------------------------------------------------------


def _str_field(desc: str) -> dict:
    return {"type": "string", "description": desc}


def build_catalogue() -> dict[str, Tool]:
    """Return the full tool registry. Built fresh per REPL session so a future
    config knob can disable individual tools without leaking state."""

    return {
        "read_remote": Tool(
            name="read_remote",
            description="Read a UTF-8 text file on a satellite (tier 1, auto-run).",
            parameters={
                "type": "object",
                "properties": {
                    "host": _str_field("Satellite host_id."),
                    "path": _str_field("Absolute path on the satellite."),
                },
                "required": ["host", "path"],
            },
            tier=Tier.SAFE,
            fn=_read_remote,
        ),
        "write_remote": Tool(
            name="write_remote",
            description="Write a text file on a satellite, scoped to a project root (tier 2).",
            parameters={
                "type": "object",
                "properties": {
                    "host": _str_field("Satellite host_id."),
                    "path": _str_field("Absolute path on the satellite."),
                    "content": _str_field("UTF-8 file content."),
                    "project_root": _str_field("Project root on the satellite, for tier scoping."),
                },
                "required": ["host", "path", "content"],
            },
            tier=Tier.PROJECT,
            fn=_write_remote,
        ),
        "run_on": Tool(
            name="run_on",
            description=(
                "Run a shell command on a satellite. Tier is computed from the "
                "command via the standard safe/project/destructive classifier."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "host": _str_field("Satellite host_id."),
                    "command": _str_field("Shell command line."),
                    "cwd": _str_field("Working directory on the satellite."),
                    "project_root": _str_field("Project root for permissions scoping."),
                    "timeout_s": {"type": "number", "description": "Timeout in seconds."},
                },
                "required": ["host", "command"],
            },
            tier=Tier.PROJECT,  # baseline; reclassified per call
            fn=_run_on,
            needs_command_classification=True,
        ),
        "apply_diff_remote": Tool(
            name="apply_diff_remote",
            description="Apply a unified diff to a project on a satellite, then commit.",
            parameters={
                "type": "object",
                "properties": {
                    "host": _str_field("Satellite host_id."),
                    "project_root": _str_field("Project root on the satellite."),
                    "diff": _str_field("Unified-diff text."),
                    "commit_message": _str_field("Commit message."),
                },
                "required": ["host", "project_root", "diff", "commit_message"],
            },
            tier=Tier.PROJECT,
            fn=_apply_diff_remote,
        ),
        "register_project": Tool(
            name="register_project",
            description=(
                "Create or update a project record on the hub. CALL THIS "
                "when the user wants to set up a new project ('make a "
                "project for the scRNA-seq work', 'register a project "
                "called …'). Pair with add_project_location to attach "
                "the (host, path) pairs the project spans."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "id": _str_field("Stable slug."),
                    "name": _str_field("Human-readable name (defaults to id)."),
                },
                "required": ["id"],
            },
            tier=Tier.SAFE,
            fn=_register_project,
        ),
        "add_project_location": Tool(
            name="add_project_location",
            description=(
                "Attach a (host, path) location to a project. CALL THIS "
                "whenever the user describes where a project lives — "
                "phrases like 'data is on the GPU workstation at /data/x', "
                "'location = workstation /scratch/y', 'the laptop has the "
                "notebooks under …', or 'add another location for this "
                "project'. The host argument is a satellite host_id from "
                "the enrolled-hosts registry (see `baird satellite list`); "
                "the path is the absolute path on that satellite. Do NOT "
                "instead propose a diff against project.yaml — locations "
                "live on the hub and only this tool persists them."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "project_id": _str_field("Project id."),
                    "host": _str_field("Satellite host_id."),
                    "path": _str_field("Absolute path on the satellite."),
                    "role": _str_field("Optional role tag (data | compute | notebook | repo)."),
                },
                "required": ["project_id", "host", "path"],
            },
            tier=Tier.SAFE,
            fn=_add_project_location,
        ),
        "list_projects": Tool(
            name="list_projects",
            description="List all projects registered with BAIRD on the hub.",
            parameters={
                "type": "object",
                "properties": {},
            },
            tier=Tier.SAFE,
            fn=_list_projects,
        ),
        "list_project_locations": Tool(
            name="list_project_locations",
            description="List all (host, path) locations for a project.",
            parameters={
                "type": "object",
                "properties": {"project_id": _str_field("Project id.")},
                "required": ["project_id"],
            },
            tier=Tier.SAFE,
            fn=_list_project_locations,
        ),
        "set_watch_root": Tool(
            name="set_watch_root",
            description=(
                "Edit a satellite's host.yaml `watch.roots` to point at a path "
                "and restart its baird-daemon user unit. Tier 2 — autorun with warning."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "host": _str_field("Satellite host_id."),
                    "path": _str_field("New watch root on the satellite."),
                },
                "required": ["host", "path"],
            },
            tier=Tier.PROJECT,
            fn=_set_watch_root,
        ),
        "install_env": Tool(
            name="install_env",
            description=(
                "Install an environment spec on a satellite (conda env file or pip "
                "requirements). Tier 3 — always prompts before running."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "host": _str_field("Satellite host_id."),
                    "project_id": _str_field("Project id for provenance."),
                    "env_spec": _str_field("environment.yml body OR pip requirements list."),
                },
                "required": ["host", "project_id", "env_spec"],
            },
            tier=Tier.DESTRUCTIVE,
            fn=_install_env,
        ),
        "where": Tool(
            name="where",
            description=(
                "Resolve a data alias or path fragment against the active project's "
                "locations and data_aliases — and across its parent + sibling "
                "projects when the project sits under an umbrella (one-level "
                "hierarchy). CALL THIS when the user asks 'where is X', 'which "
                "host has the …', or references data from a sibling assay "
                "('the scRNA cohort', 'the spatial counts'). The `project_id` "
                "field on each hit names the family member the data came from."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": _str_field("Free-text query (alias name or path fragment)."),
                    "project_id": _str_field("Project id; defaults to the active one."),
                },
                "required": ["query"],
            },
            tier=Tier.SAFE,
            fn=_where,
        ),
        "list_siblings": Tool(
            name="list_siblings",
            description=(
                "List sibling project ids — other children of the same parent. "
                "CALL THIS when the user references another assay in the same "
                "research programme ('what other assays are under umbrella programme?', "
                "'list the other cohorts'), or when you need to know which "
                "family members `where` would search. Empty list for top-level "
                "projects (no parent → no siblings)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "project_id": _str_field("Project id; defaults to the active one."),
                },
                "required": [],
            },
            tier=Tier.SAFE,
            fn=_list_siblings,
        ),
    }


# ---- Dispatcher -------------------------------------------------------


@dataclass
class DispatchResult:
    ok: bool
    tier: Tier
    decision_reason: str
    result: Any = None
    error: str | None = None


def classify_tool_call(
    tool: Tool, args: dict[str, Any], *, project_root: str | None = None
) -> Decision:
    """Return the effective tier for a single tool call.

    - `run_on` reclassifies using the command-line classifier (so a `run_on`
      call with `command="rm -rf /"` correctly lands in tier 3 even though
      the tool's baseline is tier 2).
    - `write_remote` reclassifies using the write classifier (path in project
      root → tier 2; outside → tier 3).
    - Other tools use their declared tier.
    """
    from pathlib import Path as _Path

    if tool.needs_command_classification:
        cmd = args.get("command", "")
        return classify_command(cmd)
    if tool.name == "write_remote":
        target = args.get("path", "")
        root = args.get("project_root") or project_root
        return classify_write(
            _Path(target),
            project_root=_Path(root) if root else None,
        )
    return Decision(tier=tool.tier, reason=f"tool {tool.name} declared tier")


@dataclass
class ApprovalGate:
    """Callable that decides whether a tier-2/3 call may proceed.

    Default: tier-1 auto-run, tier-2 auto-run-with-log, tier-3 always-block.
    The REPL plugs in a prompt-the-user variant.
    """

    auto_safe: bool = True
    auto_project: bool = True
    auto_destructive: bool = False
    on_warn: Callable[[str], None] | None = None

    def allow(self, tool: Tool, decision: Decision) -> bool:
        if decision.tier == Tier.SAFE:
            return self.auto_safe
        if decision.tier == Tier.PROJECT:
            if self.on_warn is not None and not self.auto_destructive:
                self.on_warn(
                    f"{tool.name}: tier 2 ({decision.reason}) — auto-running"
                )
            return self.auto_project
        return self.auto_destructive


def dispatch(
    tool: Tool,
    args: dict[str, Any],
    env: ToolEnv,
    *,
    gate: ApprovalGate | None = None,
    project_root: str | None = None,
) -> DispatchResult:
    gate = gate or ApprovalGate()
    decision = classify_tool_call(tool, args, project_root=project_root)
    if not gate.allow(tool, decision):
        return DispatchResult(
            ok=False,
            tier=decision.tier,
            decision_reason=decision.reason,
            error=f"blocked at tier {decision.tier.value}",
        )
    try:
        out = tool.fn(env, **args)
    except ToolError as e:
        return DispatchResult(
            ok=False, tier=decision.tier, decision_reason=decision.reason, error=str(e)
        )
    return DispatchResult(
        ok=True, tier=decision.tier, decision_reason=decision.reason, result=out
    )


def tools_openai_schema(catalogue: dict[str, Tool] | None = None) -> list[dict[str, Any]]:
    """Render the tool catalogue in OpenAI function-calling schema, suitable
    for passing as `tools=[...]` on a chat completions request.

    The `parameters` field on each Tool already conforms to JSONSchema, so the
    transform is a thin wrapper. Models that support OpenAI-style tool calling
    (most current frontier models + many on OpenRouter) can then emit
    structured `tool_calls` instead of free-form text the agent has to parse.
    """
    cat = catalogue if catalogue is not None else build_catalogue()
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in cat.values()
    ]


def tool_catalogue_prompt(catalogue: dict[str, Tool] | None = None) -> str:
    """Render the tool catalogue as a system-prompt block.

    Embedded in the per-turn system prompt so the model sees what verbs are
    available — without this the model defaults to proposing diffs against
    project.yaml even when a dedicated tool (e.g. add_project_location)
    exists. Each entry shows the name, the (full) description, and the
    required + optional argument names.
    """
    cat = catalogue if catalogue is not None else build_catalogue()
    lines: list[str] = [
        "## Available hub tools",
        "",
        "Prefer calling a tool below over proposing a diff against project "
        "memory. Tools persist state through the hub; diffs against "
        "project.yaml are ignored for state the hub owns (locations, "
        "decisions, environment installs, satellite host.yaml).",
        "",
    ]
    for name in sorted(cat):
        tool = cat[name]
        params = tool.parameters.get("properties", {}) or {}
        required = set(tool.parameters.get("required", []) or [])
        req_args = ", ".join(f"{p}" for p in params if p in required)
        opt_args = ", ".join(f"{p}" for p in params if p not in required)
        sig = req_args + (f" [+ optional: {opt_args}]" if opt_args else "")
        lines.append(f"- **{name}**({sig}): {tool.description}")
    return "\n".join(lines)


__all__ = [
    "Tool",
    "ToolEnv",
    "ToolError",
    "ApprovalGate",
    "DispatchResult",
    "build_catalogue",
    "classify_tool_call",
    "tools_openai_schema",
    "dispatch",
    "tool_catalogue_prompt",
]
