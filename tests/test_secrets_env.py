"""secrets.env loading."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from baird import paths


def test_load_missing_returns_empty(tmp_path: Path) -> None:
    assert paths.load_secrets_env(tmp_path / "nope.env") == {}


def test_load_parses_simple_kv(tmp_path: Path) -> None:
    p = tmp_path / "secrets.env"
    p.write_text("FOO=bar\n# comment\nBAZ=qux quux\n")
    out = paths.load_secrets_env(p)
    assert out == {"FOO": "bar", "BAZ": "qux quux"}


def test_load_strips_matched_quotes(tmp_path: Path) -> None:
    p = tmp_path / "s.env"
    p.write_text('A="hello"\nB=\'world\'\nC="mismatched\'\n')
    out = paths.load_secrets_env(p)
    assert out["A"] == "hello"
    assert out["B"] == "world"
    assert out["C"] == '"mismatched\''


def test_apply_does_not_overwrite_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-shell")
    p = tmp_path / "s.env"
    p.write_text("OPENROUTER_API_KEY=from-file\nNEW_ONE=yes\n")
    added = paths.apply_secrets_env(p)
    assert "NEW_ONE" in added
    assert "OPENROUTER_API_KEY" not in added
    assert os.environ["OPENROUTER_API_KEY"] == "from-shell"
    assert os.environ["NEW_ONE"] == "yes"
