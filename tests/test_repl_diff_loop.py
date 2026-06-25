"""Tests for the REPL diff-block detection + per-block apply prompt."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rich.console import Console

from baird.context_loader import RepoContext
from baird.memory_client import HubClient
from baird.model import OpenRouterClient
from baird.project_yaml import project_yaml_template
from baird.repl import ReplConfig, extract_diff_blocks, run_repl


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture
def repo_proj(tmp_path: Path) -> Path:
    root = tmp_path / "p"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.test")
    _git(root, "config", "user.name", "tester")
    (root / "a.txt").write_text("hello\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _ctx_for(root: Path) -> RepoContext:
    return RepoContext(
        project=project_yaml_template("p", "P"),
        project_root=root,
        branch="main",
        git_log_lines=[],
        git_status="",
        tree="",
        relevant_files={},
        decisions=[],
        action_summaries=[],
        rules_summary=[],
        host_id="t",
    )


# ---- extract_diff_blocks ----------------------------------------------


def test_extract_diff_blocks_finds_diff_fence() -> None:
    text = "before\n```diff\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n```\nafter"
    blocks = extract_diff_blocks(text)
    assert len(blocks) == 1
    assert "+2" in blocks[0]


def test_extract_diff_blocks_finds_patch_fence_too() -> None:
    text = "```patch\nstuff\n```\n```python\nnot a diff\n```\n```diff\nmore\n```"
    blocks = extract_diff_blocks(text)
    assert len(blocks) == 2


def test_extract_diff_blocks_no_fence_returns_empty() -> None:
    assert extract_diff_blocks("plain text") == []


# ---- REPL with diff loop ----------------------------------------------


def _model_with_diff(reply_with_diff: str):
    def t(_req):
        return {
            "choices": [{"message": {"content": reply_with_diff}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        }
    return OpenRouterClient(transport=t)


def test_repl_apply_diff_on_y(repo_proj: Path, client: TestClient) -> None:
    reply = (
        "Here's the change:\n\n"
        "```diff\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-hello\n"
        "+goodbye\n"
        "```\n"
    )
    hub = _Hub(client)
    console = Console(record=True, width=120)
    run_repl(
        repo_ctx=_ctx_for(repo_proj),
        hub=hub,
        model_client=_model_with_diff(reply),
        config=ReplConfig(project_id="p", project_root=repo_proj),
        console=console,
        inputs=["please change it", "y", "/exit"],
    )
    assert (repo_proj / "a.txt").read_text() == "goodbye\n"


def test_repl_skips_diff_when_user_says_no(repo_proj: Path, client: TestClient) -> None:
    reply = (
        "```diff\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-hello\n"
        "+nope\n"
        "```\n"
    )
    hub = _Hub(client)
    console = Console(record=True, width=120)
    run_repl(
        repo_ctx=_ctx_for(repo_proj),
        hub=hub,
        model_client=_model_with_diff(reply),
        config=ReplConfig(project_id="p", project_root=repo_proj),
        console=console,
        inputs=["change it", "n", "/exit"],
    )
    assert (repo_proj / "a.txt").read_text() == "hello\n"
    assert "skipped" in console.export_text()


def test_repl_diff_loop_disabled_by_config(repo_proj: Path, client: TestClient) -> None:
    reply = (
        "```diff\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-hello\n"
        "+goodbye\n"
        "```\n"
    )
    hub = _Hub(client)
    console = Console(record=True, width=120)
    run_repl(
        repo_ctx=_ctx_for(repo_proj),
        hub=hub,
        model_client=_model_with_diff(reply),
        config=ReplConfig(
            project_id="p", project_root=repo_proj, diff_loop_enabled=False
        ),
        console=console,
        inputs=["x", "/exit"],
    )
    assert (repo_proj / "a.txt").read_text() == "hello\n"


def test_repl_no_diff_command_disables_prompt(repo_proj: Path, client: TestClient) -> None:
    reply = (
        "```diff\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-hello\n"
        "+x\n"
        "```\n"
    )
    hub = _Hub(client)
    console = Console(record=True, width=120)
    run_repl(
        repo_ctx=_ctx_for(repo_proj),
        hub=hub,
        model_client=_model_with_diff(reply),
        config=ReplConfig(project_id="p", project_root=repo_proj),
        console=console,
        inputs=["/no-diff", "x", "/exit"],
    )
    assert (repo_proj / "a.txt").read_text() == "hello\n"


def test_repl_apply_failure_reported(repo_proj: Path, client: TestClient) -> None:
    """A diff that doesn't apply should print an error but not crash the loop."""
    bad_reply = "```diff\nthis is not a real diff\n```\n"
    hub = _Hub(client)
    console = Console(record=True, width=120)
    run_repl(
        repo_ctx=_ctx_for(repo_proj),
        hub=hub,
        model_client=_model_with_diff(bad_reply),
        config=ReplConfig(project_id="p", project_root=repo_proj),
        console=console,
        inputs=["go", "y", "/exit"],
    )
    assert "apply failed" in console.export_text()
