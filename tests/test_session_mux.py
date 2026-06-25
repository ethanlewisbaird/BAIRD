"""Tests for the session multiplexer abstraction.

We focus on the bits that don't actually need tmux/screen installed: backend
selection, the noop backend, and the deterministic naming helper. Real
tmux/screen invocations would shell out — skipped when the binaries are
absent (most CI/test environments don't have them)."""

from __future__ import annotations

import shutil

import pytest

from baird.session_mux import (
    MultiplexerError,
    NoopBackend,
    ScreenBackend,
    TmuxBackend,
    deterministic_session_name,
    select_backend,
)


def test_deterministic_name_stable() -> None:
    n1 = deterministic_session_name(prefix="baird", scope="proj", action_id="abcdef1234567890")
    n2 = deterministic_session_name(prefix="baird", scope="proj", action_id="abcdef1234567890")
    assert n1 == n2 == "baird-proj-abcdef12"


def test_select_backend_none_returns_noop() -> None:
    mux = select_backend("none")
    assert isinstance(mux, NoopBackend)


def test_select_backend_tmux_missing_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(MultiplexerError):
        select_backend("tmux")


def test_select_backend_screen_missing_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(MultiplexerError):
        select_backend("screen")


def test_auto_falls_back_to_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    mux = select_backend("auto")
    assert isinstance(mux, NoopBackend)


def test_auto_prefers_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    mux = select_backend("auto")
    assert isinstance(mux, TmuxBackend)


def test_auto_falls_back_to_screen_when_no_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}" if name == "screen" else None)
    mux = select_backend("auto")
    assert isinstance(mux, ScreenBackend)


def test_noop_round_trip() -> None:
    mux = NoopBackend()
    info = mux.create_session(name="x")
    assert info.backend == "none"
    assert mux.list_sessions() == []
    assert mux.kill(name="x") is True


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_tmux_create_send_kill_round_trip() -> None:
    mux = TmuxBackend()
    name = "baird-test-roundtrip"
    mux.kill(name=name)  # ensure clean
    info = mux.create_session(name=name)
    assert info.name == name
    try:
        names = {s.name for s in mux.list_sessions()}
        assert name in names
        mux.send(name=name, command="true")
    finally:
        mux.kill(name=name)
