"""Hub/daemon systemd unit install. systemctl + sudo are stubbed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from baird import hub_install


class _Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.default = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def __call__(self, cmd, *, input=None):
        self.calls.append((cmd, input))
        return self.default


def _spec(tmp_path: Path, scope: str) -> hub_install.InstallSpec:
    return hub_install.InstallSpec(
        scope=scope,  # type: ignore[arg-type]
        baird_bin="/opt/baird/.venv/bin/baird",
        baird_home=tmp_path / ".baird",
        home=tmp_path,
        user="ethan",
        system_unit_dir=tmp_path / "etc-systemd",
        user_unit_dir=tmp_path / "user-systemd",
    )


def test_render_units_user_scope(tmp_path: Path) -> None:
    units = hub_install.render_units(_spec(tmp_path, "user"))
    hub = units["baird-hub.service"]
    daemon = units["baird-daemon.service"]
    # User scope omits explicit User= / Group= and targets default.target.
    assert "User=" not in hub
    assert "WantedBy=default.target" in hub
    assert "ExecStart=/opt/baird/.venv/bin/baird hub serve" in hub
    assert f"Environment=BAIRD_HOME={tmp_path}/.baird" in hub
    assert "ExecStart=/opt/baird/.venv/bin/baird daemon" in daemon
    assert "Requires=baird-hub.service" in daemon


def test_render_units_system_scope_sets_user_and_target(tmp_path: Path) -> None:
    hub = hub_install.render_units(_spec(tmp_path, "system"))["baird-hub.service"]
    assert "User=ethan" in hub
    assert "Group=ethan" in hub
    assert "WantedBy=multi-user.target" in hub


def test_install_user_writes_files_and_enables(tmp_path: Path) -> None:
    runner = _Runner()
    spec = _spec(tmp_path, "user")
    units = hub_install.install(spec, run=runner)

    assert units == ["baird-hub.service", "baird-daemon.service"]
    user_dir = tmp_path / "user-systemd"
    assert (user_dir / "baird-hub.service").exists()
    assert (user_dir / "baird-daemon.service").exists()

    cmd_strs = [" ".join(c[0]) for c in runner.calls]
    assert "systemctl --user daemon-reload" in cmd_strs
    assert (
        "systemctl --user enable --now baird-hub.service baird-daemon.service"
        in cmd_strs
    )
    # No sudo on the --user path.
    assert not any(c[0][0] == "sudo" for c in runner.calls)


def test_install_system_uses_sudo_tee(tmp_path: Path) -> None:
    runner = _Runner()
    spec = _spec(tmp_path, "system")
    hub_install.install(spec, run=runner)

    tee_calls = [c for c in runner.calls if c[0][:2] == ["sudo", "tee"]]
    assert len(tee_calls) == 2
    # Unit body is piped via stdin to tee.
    bodies = {c[1] for c in tee_calls}
    assert any("ExecStart=/opt/baird/.venv/bin/baird hub serve" in (b or "") for b in bodies)
    assert any("ExecStart=/opt/baird/.venv/bin/baird daemon" in (b or "") for b in bodies)

    cmd_strs = [" ".join(c[0]) for c in runner.calls]
    assert "sudo systemctl daemon-reload" in cmd_strs
    assert (
        "sudo systemctl enable --now baird-hub.service baird-daemon.service" in cmd_strs
    )


def test_install_propagates_systemctl_failure(tmp_path: Path) -> None:
    class _Fail(_Runner):
        def __call__(self, cmd, *, input=None):
            self.calls.append((cmd, input))
            if "daemon-reload" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=1, stderr="nope", stdout="")
            return self.default

    spec = _spec(tmp_path, "user")
    with pytest.raises(RuntimeError, match="nope"):
        hub_install.install(spec, run=_Fail())


def test_uninstall_disables_and_removes_user_units(tmp_path: Path) -> None:
    runner = _Runner()
    spec = _spec(tmp_path, "user")
    # Pre-create files so we know uninstall removes them.
    spec.user_unit_dir.mkdir(parents=True)
    (spec.user_unit_dir / "baird-hub.service").write_text("x")
    (spec.user_unit_dir / "baird-daemon.service").write_text("x")

    hub_install.uninstall(spec, run=runner)

    assert not (spec.user_unit_dir / "baird-hub.service").exists()
    assert not (spec.user_unit_dir / "baird-daemon.service").exists()
    cmd_strs = [" ".join(c[0]) for c in runner.calls]
    assert (
        "systemctl --user disable --now baird-hub.service baird-daemon.service"
        in cmd_strs
    )
