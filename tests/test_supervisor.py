"""Hub auto-start supervisor."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from baird import supervisor


def test_is_hub_running_false_when_no_hub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    # Use a port that's unlikely to be bound.
    (tmp_path / "config.yaml").write_text("listen: 127.0.0.1:59231\n")
    assert supervisor.is_hub_running(timeout=0.2) is False


def test_stop_hub_returns_false_with_no_pid_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    # Disable the pkill fallback so a live hub on the dev machine doesn't get
    # SIGTERM'd by the test (and the assertion stays meaningful).
    monkeypatch.setattr(supervisor, "_pkill_pattern", lambda _p: False)
    assert supervisor.stop_hub() is False


def test_is_daemon_running_false_when_no_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    assert supervisor.is_daemon_running() is False


def test_stop_daemon_returns_false_with_no_pid_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "_pkill_pattern", lambda _p: False)
    assert supervisor.stop_daemon() is False


def test_pkill_pattern_fallback_kills_unsupervised_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When no PID file exists, stop_hub should fall back to a pattern-based
    pkill so manually-launched hubs can still be reaped."""
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    called: list[str] = []
    monkeypatch.setattr(
        supervisor,
        "_pkill_pattern",
        lambda pattern: called.append(pattern) or True,
    )
    assert supervisor.stop_hub() is True
    assert any("hub serve" in p for p in called)


def test_is_daemon_running_handles_stale_pid_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pid file pointing at a dead process should report not-running, not
    raise. Use a very large PID that's almost certainly free."""
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    (tmp_path / "daemon.pid").write_text("2147483646")
    assert supervisor.is_daemon_running() is False


def test_ensure_then_stop_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: spawn a hub, hit it, then stop it."""
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("listen: 127.0.0.1:59232\n")

    supervisor.ensure_hub_running(wait_s=10.0, quiet=True)
    try:
        assert supervisor.is_hub_running(timeout=1.0)
        pid_file = tmp_path / "hub.pid"
        assert pid_file.exists()
    finally:
        assert supervisor.stop_hub()
    # Give the process a moment to die.
    for _ in range(20):
        if not supervisor.is_hub_running(timeout=0.2):
            return
        time.sleep(0.2)
    pytest.fail("hub did not stop within 4s")
