"""MCP (Model Context Protocol) client integration.

Lets BAIRD call out to MCP servers — PubMed, bioRxiv, ChEMBL, etc. — from
inside the research / coding loops. The MCP SDK is async-only; we wrap each
call in `asyncio.run` so the rest of BAIRD stays synchronous.

Server config lives at `<baird_home>/mcp_servers.yaml`:

    servers:
      - id: pubmed
        command: uvx
        args: [pubmed-mcp]
        env:
          PUBMED_EMAIL: you@example.com
        enabled: true
      - id: bioRxiv
        command: node
        args: [/path/to/biorxiv-mcp/server.js]

This module is gated behind `pip install baird[mcp]`. Missing deps → empty
list, never crashes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import paths


log = logging.getLogger(__name__)


@dataclass
class ServerSpec:
    id: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    description: str = ""


@dataclass
class ToolSpec:
    server_id: str
    name: str
    description: str
    input_schema: dict[str, Any]


def config_path() -> Path:
    return paths.baird_home() / "mcp_servers.yaml"


def load_servers(path: Path | None = None) -> list[ServerSpec]:
    p = path or config_path()
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        log.warning("mcp_servers.yaml parse error: %s", e)
        return []
    raw = data.get("servers", [])
    out: list[ServerSpec] = []
    for entry in raw:
        if not isinstance(entry, dict) or "id" not in entry or "command" not in entry:
            continue
        out.append(ServerSpec(
            id=entry["id"],
            command=entry["command"],
            args=list(entry.get("args", [])),
            env=dict(entry.get("env", {})),
            enabled=bool(entry.get("enabled", True)),
            description=str(entry.get("description", "")),
        ))
    return [s for s in out if s.enabled]


def save_servers(specs: list[ServerSpec], path: Path | None = None) -> None:
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({
        "servers": [
            {
                "id": s.id, "command": s.command, "args": s.args,
                "env": s.env, "enabled": s.enabled,
                "description": s.description,
            }
            for s in specs
        ],
    }, sort_keys=False))


# ---------- async core (not directly called) ----------------------------


async def _aopen(spec: ServerSpec):
    """Open an MCP stdio session against `spec`. Returns the session +
    teardown function. Caller must await teardown."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=spec.command, args=spec.args, env=spec.env or None
    )
    stdio = stdio_client(params)
    read, write = await stdio.__aenter__()
    session = ClientSession(read, write)
    await session.__aenter__()
    await session.initialize()
    return session, stdio


async def _aclose(session, stdio):
    await session.__aexit__(None, None, None)
    await stdio.__aexit__(None, None, None)


async def _alist_tools(spec: ServerSpec) -> list[ToolSpec]:
    session, stdio = await _aopen(spec)
    try:
        listing = await session.list_tools()
        out: list[ToolSpec] = []
        for t in listing.tools:
            out.append(ToolSpec(
                server_id=spec.id,
                name=t.name,
                description=getattr(t, "description", "") or "",
                input_schema=getattr(t, "inputSchema", {}) or {},
            ))
        return out
    finally:
        await _aclose(session, stdio)


async def _acall_tool(spec: ServerSpec, name: str, args: dict[str, Any]) -> Any:
    session, stdio = await _aopen(spec)
    try:
        result = await session.call_tool(name, args)
        # MCP's CallToolResult has `.content` (list of content blocks).
        out: list[Any] = []
        for block in getattr(result, "content", []):
            if hasattr(block, "text"):
                out.append(block.text)
            elif hasattr(block, "data"):
                out.append(block.data)
            else:
                out.append(str(block))
        return "\n".join(str(x) for x in out)
    finally:
        await _aclose(session, stdio)


# ---------- sync public API ---------------------------------------------


def list_tools(spec: ServerSpec, *, timeout: float = 15.0) -> list[ToolSpec]:
    """List tools the server exposes. Empty list on any failure."""
    try:
        return asyncio.run(asyncio.wait_for(_alist_tools(spec), timeout=timeout))
    except Exception as e:
        log.warning("mcp list_tools(%s) failed: %s", spec.id, e)
        return []


def call_tool(
    spec: ServerSpec, tool: str, args: dict[str, Any], *, timeout: float = 60.0
) -> str:
    """Call a tool synchronously. Returns the joined text content. Empty
    string on failure."""
    try:
        return asyncio.run(
            asyncio.wait_for(_acall_tool(spec, tool, args), timeout=timeout)
        )
    except Exception as e:
        log.warning("mcp call_tool(%s, %s) failed: %s", spec.id, tool, e)
        return ""


def ping(spec: ServerSpec, *, timeout: float = 10.0) -> bool:
    """Quick reachability check: open a session, list tools, close. True if
    the server responded at all."""
    try:
        tools = asyncio.run(asyncio.wait_for(_alist_tools(spec), timeout=timeout))
        return len(tools) >= 0  # any successful return counts
    except Exception:
        return False


# ---------- discovery helpers used by /research -------------------------


def all_tools(specs: list[ServerSpec] | None = None) -> list[ToolSpec]:
    """All tools from all enabled servers. Failures per-server are silently
    skipped so a misbehaving server doesn't blank the whole list."""
    out: list[ToolSpec] = []
    for spec in (specs if specs is not None else load_servers()):
        out.extend(list_tools(spec))
    return out


def find_server(server_id: str, specs: list[ServerSpec] | None = None) -> ServerSpec | None:
    for s in (specs if specs is not None else load_servers()):
        if s.id == server_id:
            return s
    return None


# ---------- ToolRegistry integration --------------------------------------


def register_server_tools(
    spec: ServerSpec,
    tool_registry: Any,
    *,
    timeout: float = 15.0,
) -> list[str]:
    """Discover tools from an MCP server and register them with a BAIRD
    `ToolRegistry`. Returns the list of registered tool names."""
    from .agent_tools import Tier, Tool

    mcp_tools = list_tools(spec, timeout=timeout)
    registered: list[str] = []
    for mt in mcp_tools:

        def _make_call(_spec: ServerSpec, _name: str):
            def _call(env, **kwargs) -> Any:
                return call_tool(_spec, _name, kwargs)
            return _call

        tool = Tool(
            name=f"{spec.id}_{mt.name}",
            description=mt.description + f" (MCP server: {spec.id})",
            parameters={
                "type": "object",
                "properties": {
                    k: v for k, v in mt.input_schema.get("properties", {}).items()
                },
                "required": mt.input_schema.get("required", []),
            },
            tier=Tier.SAFE,
            fn=_make_call(spec, mt.name),
        )
        tool_registry.register(tool)
        registered.append(tool.name)
    return registered
