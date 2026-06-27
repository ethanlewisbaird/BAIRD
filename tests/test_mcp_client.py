"""MCP server config + sync wrapper around the async SDK.

The actual MCP SDK calls subprocess; tests only exercise config loading
and the routing parser in research.py. The sync wrappers (list_tools,
call_tool, ping) are smoke-tested only when a real server is configured.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_load_servers_missing_file(tmp_path: Path) -> None:
    from baird.mcp_client import load_servers

    assert load_servers(tmp_path / "nope.yaml") == []


def test_load_servers_parses_minimal(tmp_path: Path) -> None:
    from baird.mcp_client import load_servers

    p = tmp_path / "mcp.yaml"
    p.write_text("""
servers:
  - id: pubmed
    command: uvx
    args: [pubmed-mcp]
    env:
      PUBMED_EMAIL: you@example.com
  - id: turned-off
    command: x
    enabled: false
""")
    out = load_servers(p)
    assert [s.id for s in out] == ["pubmed"]  # disabled one filtered
    pm = out[0]
    assert pm.command == "uvx"
    assert pm.args == ["pubmed-mcp"]
    assert pm.env == {"PUBMED_EMAIL": "you@example.com"}


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    from baird.mcp_client import ServerSpec, load_servers, save_servers

    specs = [
        ServerSpec(id="a", command="cmd", args=["x"], description="desc"),
        ServerSpec(id="b", command="cmd2"),
    ]
    p = tmp_path / "mcp.yaml"
    save_servers(specs, p)
    loaded = load_servers(p)
    assert {s.id for s in loaded} == {"a", "b"}


def test_find_server() -> None:
    from baird.mcp_client import ServerSpec, find_server

    specs = [ServerSpec(id="x", command="c"), ServerSpec(id="y", command="c")]
    assert find_server("x", specs).id == "x"
    assert find_server("missing", specs) is None


def test_parse_routed_subqueries_happy() -> None:
    from baird.research import _parse_routed_subqueries

    out = _parse_routed_subqueries(
        '{"queries":[{"q":"scrna benchmarks","tool":"pubmed.search_articles"},'
        '{"q":"recent integration","tool":"web"}]}'
    )
    assert out == [
        ("scrna benchmarks", "pubmed.search_articles"),
        ("recent integration", "web"),
    ]


def test_parse_routed_subqueries_handles_fenced_json() -> None:
    from baird.research import _parse_routed_subqueries

    out = _parse_routed_subqueries(
        '```json\n{"queries":[{"q":"a","tool":"web"}]}\n```'
    )
    assert out == [("a", "web")]


def test_research_routes_to_mcp_when_planner_picks_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Drive `run_research` with a fake planner that names an MCP tool.
    The gather phase should call mcp_client.call_tool, not web_search."""
    from fastapi.testclient import TestClient

    from baird.config import HubConfig
    from baird.hub import create_app
    from baird.mcp_client import ServerSpec
    from baird.memory_client import HubClient
    from baird.model import OpenRouterClient
    from baird.research import run_research

    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    cfg = HubConfig(
        registry_db=str(tmp_path / "r.sqlite"),
        memory_db=str(tmp_path / "m.sqlite"),
        recall_enabled=False,
    )
    raw = TestClient(create_app(cfg))

    class _Hub(HubClient):
        def __init__(self, raw):
            self._client = raw

    # The model returns: planner picks the MCP tool; synth gives a brief.
    replies = iter([
        '{"queries":[{"q":"scrna benchmarks 2026","tool":"pubmed.search"}]}',
        "Synthesised brief here.",
    ])

    def t(req):
        return {
            "choices": [{"message": {"content": next(replies)}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "cost": 0.0001},
        }

    # Fake MCP: list_tools returns one tool, call_tool returns a snippet.
    from baird import mcp_client

    monkeypatch.setattr(
        mcp_client, "list_tools",
        lambda spec, **kw: [
            mcp_client.ToolSpec(
                server_id=spec.id, name="search",
                description="Search PubMed", input_schema={"properties": {"query": {}}},
            )
        ],
    )
    called: dict = {}

    def fake_call(spec, tool, args, **kw):
        called["spec"] = spec.id
        called["tool"] = tool
        called["args"] = args
        return "PubMed hit: paper title — abstract here"

    monkeypatch.setattr(mcp_client, "call_tool", fake_call)
    monkeypatch.setattr("baird.research._mcp", mcp_client, raising=False)

    def web_should_not_be_called(*a, **kw):
        raise AssertionError("web_search should not be called when MCP is chosen")

    res = run_research(
        query="best scRNA benchmarks recently",
        hub=_Hub(raw),
        model_client=OpenRouterClient(transport=t),
        web_search=web_should_not_be_called,
        mcp_servers=[ServerSpec(id="pubmed", command="x")],
    )
    assert called["spec"] == "pubmed"
    assert called["tool"] == "search"
    assert "Synthesised brief" in (res.synthesis or "")
