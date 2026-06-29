"""Tests for the multi-turn REPL."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rich.console import Console

from baird.context_loader import RepoContext
from baird.memory_client import HubClient
from baird.model import OpenRouterClient
from baird.project_yaml import project_yaml_template
from baird.repl import ReplConfig, run_repl


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


def _ctx(tmp_path: Path) -> RepoContext:
    return RepoContext(
        project=project_yaml_template("p1", "P One"),
        project_root=tmp_path,
        branch="main",
        git_log_lines=[],
        git_status="",
        tree="",
        relevant_files={},
        decisions=[],
        action_summaries=[],
        rules_summary=[],
        host_id="testhost",
    )


def _model(*replies: str):
    counter = {"i": 0}
    def t(_req):
        i = counter["i"]
        counter["i"] += 1
        reply = replies[i] if i < len(replies) else "fallback"
        return {
            "choices": [{"message": {"content": reply}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0001},
        }
    return OpenRouterClient(transport=t)


def test_repl_runs_one_turn_and_exits(tmp_path: Path, client: TestClient) -> None:
    hub = _Hub(client)
    model_client = _model("hello there")
    console = Console(record=True, width=120)
    stats = run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=model_client,
        config=ReplConfig(project_id="p1"),
        console=console,
        inputs=["hi", "/exit"],
    )
    assert stats.turns == 1
    assert "hello there" in console.export_text()


def test_repl_persists_history_across_turns(tmp_path: Path, client: TestClient) -> None:
    """Each turn re-loads the session's full message history and sends it to the model.
    With two turns there should be 4 messages in the session afterward
    (user1, asst1, user2, asst2)."""
    hub = _Hub(client)
    console = Console(record=True, width=120)
    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=_model("first", "second"),
        config=ReplConfig(project_id="p1"),
        console=console,
        inputs=["one", "two", "/exit"],
    )
    sessions = hub.list_sessions(task_id="repl-p1", mode="code")
    assert sessions
    msgs = hub.get_messages(sessions[0]["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["content"] == "one"
    assert msgs[1]["content"] == "first"


def test_repl_reset_creates_new_session(tmp_path: Path, client: TestClient) -> None:
    hub = _Hub(client)
    console = Console(record=True, width=120)
    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=_model("a", "b"),
        config=ReplConfig(project_id="p2"),
        console=console,
        inputs=["one", "/reset", "two", "/exit"],
    )
    sessions = hub.list_sessions(task_id="repl-p2", mode="code")
    assert len(sessions) == 2


def test_repl_cost_command_does_not_call_model(tmp_path: Path, client: TestClient) -> None:
    hub = _Hub(client)
    console = Console(record=True, width=120)
    called = {"n": 0}
    def t(_req):
        called["n"] += 1
        return {
            "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }
    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=OpenRouterClient(transport=t),
        config=ReplConfig(project_id="p3"),
        console=console,
        inputs=["/cost", "/exit"],
    )
    assert called["n"] == 0


def test_repl_model_command_changes_model(tmp_path: Path, client: TestClient) -> None:
    hub = _Hub(client)
    console = Console(record=True, width=120)
    seen_models: list[str] = []

    def t(req):
        seen_models.append(req["body"]["model"])
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }

    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=OpenRouterClient(transport=t),
        config=ReplConfig(project_id="p4", model="anthropic/claude-3-haiku"),
        console=console,
        inputs=["one", "/model anthropic/claude-opus-4-7", "two", "/exit"],
    )
    assert seen_models == [
        "anthropic/claude-3-haiku",
        "anthropic/claude-opus-4-7",
    ]


def test_repl_model_list_then_pick_by_index(
    tmp_path: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/model` prints the catalog; `/model 2` picks #2 from it."""
    import baird.model as model_mod

    fake_catalog = [
        {"id": "anthropic/claude-opus-4-7"},
        {"id": "anthropic/claude-sonnet-4-6"},
        {"id": "openai/gpt-4o"},
    ]
    monkeypatch.setattr(
        model_mod, "top_openrouter_models", lambda n=20: fake_catalog
    )

    hub = _Hub(client)
    console = Console(record=True, width=120)
    seen_models: list[str] = []

    def t(req):
        seen_models.append(req["body"]["model"])
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }

    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=OpenRouterClient(transport=t),
        config=ReplConfig(project_id="p5", model="anthropic/claude-3-haiku"),
        console=console,
        inputs=["/model", "/model 2", "go", "/exit"],
    )

    out = console.export_text()
    assert "anthropic/claude-opus-4-7" in out
    assert "anthropic/claude-sonnet-4-6" in out
    assert seen_models == ["anthropic/claude-sonnet-4-6"]


def test_repl_multiline_input_collapses_until_close(
    tmp_path: Path, client: TestClient
) -> None:
    hub = _Hub(client)
    console = Console(record=True, width=120)
    sent: list[list[dict]] = []

    def t(req):
        sent.append(req["body"]["messages"])
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }

    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=OpenRouterClient(transport=t),
        config=ReplConfig(project_id="p6"),
        console=console,
        inputs=['"""', "line one", "line two", "line three", '"""', "/exit"],
    )
    user_msgs = [m for batch in sent for m in batch if m["role"] == "user"]
    assert user_msgs[0]["content"] == "line one\nline two\nline three"


def test_repl_session_resume_loads_existing(
    tmp_path: Path, client: TestClient
) -> None:
    """Pass session_id explicitly and the REPL attaches without creating new."""
    hub = _Hub(client)
    s = hub.new_session(mode="code", project_id="p7", task_id="repl-p7")
    # Pre-load some history.
    hub.append_message(s["id"], role="user", content="prior turn")
    hub.append_message(s["id"], role="assistant", content="prior reply")

    sent: list[list[dict]] = []

    def t(req):
        sent.append(req["body"]["messages"])
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }

    console = Console(record=True, width=120)
    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=OpenRouterClient(transport=t),
        config=ReplConfig(project_id="p7"),
        console=console,
        inputs=["new turn", "/exit"],
        session_id=s["id"],
    )
    # Prior history should be in the messages sent.
    sent_contents = [m["content"] for batch in sent for m in batch]
    assert "prior turn" in sent_contents
    assert "prior reply" in sent_contents


def test_repl_audit_satellite_hands_off_to_model(
    tmp_path: Path, client: TestClient
) -> None:
    """Regression: `/audit-satellite <host> <path>` must inject its prompt as
    a user turn, NOT fall through to the legacy slash chain. Before the fix
    the audit prompt started with "Audit ..." and was re-parsed as `/udit`,
    landing on `unknown command: /udit`. After the fix, the model is called
    with the audit prompt and the unknown-command path never fires.
    """
    hub = _Hub(client)
    hub.upsert_project(id="p-audit", name="p-audit")
    console = Console(record=True, width=120)
    # Capture what we send to the model so we can assert on it.
    sent: list[str] = []
    def _t(req):
        # Last message in the request is the user turn for this round.
        sent.append(req["body"]["messages"][-1]["content"])
        return {
            "choices": [{"message": {"content": "audit reply"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0001},
        }
    model_client = OpenRouterClient(transport=_t)
    stats = run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=model_client,
        config=ReplConfig(project_id="p-audit"),
        console=console,
        inputs=["/audit-satellite workstation /data/raw", "/exit"],
    )
    assert stats.turns == 1
    assert sent, "the model should have been called with the audit prompt"
    assert "/data/raw" in sent[-1] and "workstation" in sent[-1]
    out = console.export_text()
    assert "unknown command" not in out
    assert "audit reply" in out


def test_repl_agent_loop_dispatches_tool_then_replies(
    tmp_path: Path, client: TestClient
) -> None:
    """End-to-end: model emits a tool_call → REPL dispatches via agent_tools →
    appends tool result → model gives final answer. The user sees [call] +
    [result] lines and the final assistant content."""
    hub = _Hub(client)
    hub.upsert_project(id="p-agent", name="p-agent")

    # Sequence of responses: first turn returns a tool_call, second returns
    # plain content. The transport inspects round number from message count.
    transport_calls: list[dict] = []
    def _t(req):
        transport_calls.append(req)
        body = req["body"]
        # Detect round by whether a tool message is already in history.
        had_tool_result = any(m.get("role") == "tool" for m in body["messages"])
        if not had_tool_result:
            # Round 1: emit a tool_call. (Use a tool from the catalogue that
            # doesn't need a satellite — `where` works against the hub.)
            return {
                "choices": [{
                    "message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "where",
                                "arguments": '{"query": "x"}',
                            },
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0001},
            }
        # Round 2: final answer.
        return {
            "choices": [{"message": {"content": "final answer"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 6, "cost": 0.0002},
        }
    model_client = OpenRouterClient(transport=_t)

    console = Console(record=True, width=160)
    stats = run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=model_client,
        config=ReplConfig(project_id="p-agent"),
        console=console,
        inputs=["go", "/exit"],
    )

    assert stats.turns == 1
    # Two upstream calls made for one user turn (tool_call, then follow-up).
    assert len(transport_calls) == 2
    # Second call must include the tool result in its messages.
    second_msgs = transport_calls[1]["body"]["messages"]
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call_1"
               for m in second_msgs)
    out = console.export_text()
    # Rich strips style tags from export_text; assert on the structured bits.
    assert "where(" in out
    assert "where:" in out
    assert "final answer" in out
    # Cost is aggregated across both rounds.
    assert stats.total_cost_usd == pytest.approx(0.0003)


def test_repl_agent_loop_blocks_destructive_tool(
    tmp_path: Path, client: TestClient
) -> None:
    """A tier-3 (destructive) tool call from the model should be blocked back
    to the model rather than auto-run. The block is reported as a `tool`
    message so the model can adapt."""
    hub = _Hub(client)
    hub.upsert_project(id="p-block", name="p-block")

    transport_calls: list[dict] = []
    def _t(req):
        transport_calls.append(req)
        body = req["body"]
        had_tool_result = any(m.get("role") == "tool" for m in body["messages"])
        if not had_tool_result:
            return {
                "choices": [{
                    "message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "call_x",
                            "type": "function",
                            "function": {
                                "name": "install_env",
                                "arguments": '{"host": "workstation", '
                                             '"project_id": "p-block", '
                                             '"env_spec": "numpy"}',
                            },
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0001},
            }
        return {
            "choices": [{"message": {"content": "ok, will leave that to you"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 6, "cost": 0.0002},
        }
    model_client = OpenRouterClient(transport=_t)

    console = Console(record=True, width=160)
    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=model_client,
        config=ReplConfig(project_id="p-block"),
        console=console,
        inputs=["please install numpy on workstation", "/exit"],
    )
    out = console.export_text()
    assert "install_env" in out
    assert "destructive" in out or "tier-3" in out
    # The tool result message sent back to the model carries the BLOCKED text.
    second_msgs = transport_calls[1]["body"]["messages"]
    tool_msg = next(
        m for m in second_msgs if m.get("role") == "tool"
    )
    assert "BLOCKED" in tool_msg["content"]


def test_repl_agent_loop_caps_at_max_rounds(
    tmp_path: Path, client: TestClient
) -> None:
    """If the model keeps emitting tool_calls forever, the loop bounds it at
    MAX_AGENT_ROUNDS and returns the latest completion (even if it still has
    tool_calls)."""
    from baird.repl import MAX_AGENT_ROUNDS

    hub = _Hub(client)
    hub.upsert_project(id="p-loop", name="p-loop")

    transport_calls = 0
    def _t(req):
        nonlocal transport_calls
        transport_calls += 1
        return {
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": f"call_{transport_calls}",
                        "type": "function",
                        "function": {
                            "name": "where",
                            "arguments": '{"query": "x"}',
                        },
                    }],
                },
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "cost": 0.0001},
        }
    model_client = OpenRouterClient(transport=_t)
    console = Console(record=True, width=120)
    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=model_client,
        config=ReplConfig(project_id="p-loop"),
        console=console,
        inputs=["go", "/exit"],
    )
    # The model is called once per round, never more than MAX_AGENT_ROUNDS.
    assert transport_calls == MAX_AGENT_ROUNDS


def test_completion_parses_tool_calls() -> None:
    """Direct unit test of Completion-side parsing."""
    client = OpenRouterClient(transport=lambda _r: {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "abc",
                    "type": "function",
                    "function": {
                        "name": "run_on",
                        "arguments": '{"host": "workstation", "command": "ls"}',
                    },
                }],
            },
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
    })
    c = client.complete(model="any", messages=[{"role": "user", "content": "hi"}])
    assert len(c.tool_calls) == 1
    assert c.tool_calls[0]["name"] == "run_on"
    assert c.tool_calls[0]["arguments"] == {"host": "workstation", "command": "ls"}


def test_contains_text_tool_call_detects_known_patterns() -> None:
    from baird.repl import contains_text_tool_call

    assert contains_text_tool_call("foo <longcat_tool_call>run_on</longcat_tool_call>")
    assert contains_text_tool_call("<tool_call>x</tool_call>")
    assert contains_text_tool_call("<function_call>y")
    assert contains_text_tool_call("```tool_call\n{}\n```")
    assert not contains_text_tool_call("just plain text")
    assert not contains_text_tool_call("")
    assert not contains_text_tool_call(None)


def test_strip_text_tool_calls_removes_markup_keeps_prose() -> None:
    from baird.repl import strip_text_tool_calls

    s = (
        "Let me check that.\n"
        "<longcat_tool_call>run_on\n"
        "<longcat_arg_key>host</longcat_arg_key>\n"
        "<longcat_arg_value>workstation</longcat_arg_value>\n"
        "</longcat_tool_call>\n"
        "Trailing text."
    )
    out = strip_text_tool_calls(s)
    assert "longcat_tool_call" not in out
    assert "Let me check that." in out
    assert "Trailing text." in out


def test_strip_handles_orphan_opening_tag() -> None:
    from baird.repl import strip_text_tool_calls

    # owl-alpha sometimes emits an opening tag without a closing one
    # (truncation, etc.). Strip from the tag onward.
    s = "Doing things now.\n<longcat_tool_call>run_on\nhost=x\ncommand=ls"
    out = strip_text_tool_calls(s)
    assert "longcat_tool_call" not in out
    assert "Doing things now." in out


def test_repl_agent_loop_recovers_from_text_tool_call(
    tmp_path: Path, client: TestClient
) -> None:
    """Self-healing: model emits text-shaped markup on round 1 (no structured
    tool_calls). Agent loop detects, appends a corrective system message,
    retries once. Round 2 returns a real structured tool_call → dispatch.
    Round 3 returns final content."""
    hub = _Hub(client)
    hub.upsert_project(id="p-drift", name="p-drift")

    round_n = {"i": 0}
    transport_calls: list[dict] = []
    def _t(req):
        transport_calls.append(req)
        round_n["i"] += 1
        if round_n["i"] == 1:
            # Text-shaped tool call — should NOT count as structured.
            return {
                "choices": [{
                    "message": {
                        "content": (
                            "I'll list things.\n"
                            "<longcat_tool_call>where\n"
                            "<longcat_arg_key>query</longcat_arg_key>\n"
                            "<longcat_arg_value>x</longcat_arg_value>\n"
                            "</longcat_tool_call>"
                        ),
                    },
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0001},
            }
        if round_n["i"] == 2:
            # After the nudge, model goes structured.
            return {
                "choices": [{
                    "message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "fix1", "type": "function",
                            "function": {"name": "where",
                                         "arguments": '{"query": "x"}'},
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 12, "completion_tokens": 6, "cost": 0.0001},
            }
        return {
            "choices": [{"message": {"content": "final result"}}],
            "usage": {"prompt_tokens": 14, "completion_tokens": 7, "cost": 0.0001},
        }

    console = Console(record=True, width=160)
    stats = run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=OpenRouterClient(transport=_t),
        config=ReplConfig(project_id="p-drift"),
        console=console,
        inputs=["go", "/exit"],
    )

    assert stats.turns == 1
    # Round 1 = text drift, round 2 = structured tool call, round 3 = final.
    assert round_n["i"] == 3
    # Round 2 request must contain the drift-correction system message.
    round2_msgs = transport_calls[1]["body"]["messages"]
    assert any(
        m.get("role") == "system" and "function-calling channel" in (m.get("content") or "")
        for m in round2_msgs
    )
    out = console.export_text()
    assert "final result" in out


def test_history_loader_strips_text_tool_calls_from_assistant(
    client: TestClient,
) -> None:
    """A previously-poisoned session can self-recover without /reset because
    `load_history_with_summary` strips text-tool-call markup from prior
    assistant messages before sending them back to the model."""
    from baird.context_compressor import load_history_with_summary

    hub = _Hub(client)
    sess = hub.find_or_create_session_for_task(
        task_id="t-drift", project_id="p-loader", mode="code"
    )
    sid = sess["id"]
    hub.append_message(sid, role="user", content="please list things")
    hub.append_message(
        sid, role="assistant",
        content="Sure.\n<longcat_tool_call>where\n"
                "<longcat_arg_key>q</longcat_arg_key>\n"
                "<longcat_arg_value>x</longcat_arg_value>\n"
                "</longcat_tool_call>",
    )

    out = load_history_with_summary(
        hub, session_id=sid, cap=20,
        model_client=OpenRouterClient(transport=lambda r: {
            "choices": [{"message": {"content": ""}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }),
    )
    assistant_msg = next(m for m in out if m["role"] == "assistant")
    assert "longcat_tool_call" not in assistant_msg["content"]
    assert "Sure." in assistant_msg["content"]


def test_repl_records_action_per_turn(tmp_path: Path, client: TestClient) -> None:
    hub = _Hub(client)
    console = Console(record=True, width=120)
    run_repl(
        repo_ctx=_ctx(tmp_path),
        hub=hub,
        model_client=_model("r1", "r2"),
        config=ReplConfig(project_id="p4", model="anthropic/claude-3-haiku"),
        console=console,
        inputs=["a", "b", "/exit"],
    )
    actions = hub.list_actions(project_id="p4")
    assert len(actions) == 2
    for a in actions:
        assert a["model_name"] == "anthropic/claude-3-haiku"
        assert a["cost_usd"] == pytest.approx(0.0001)
        assert a["exit_code"] == 0
