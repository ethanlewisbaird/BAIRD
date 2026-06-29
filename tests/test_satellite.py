"""Satellite enrol + tunnel install logic. SSH/systemctl are stubbed."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from baird import satellite


class _Runner:
    """Stub CommandRunner. Records calls; returns scripted exit/stdout."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Optional[str]]] = []
        self._scripted: dict[tuple[str, ...], subprocess.CompletedProcess] = {}
        self.default = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def script(self, cmd_prefix: tuple[str, ...], result: subprocess.CompletedProcess) -> None:
        self._scripted[cmd_prefix] = result

    def __call__(
        self, cmd: list[str], *, input: Optional[str] = None
    ) -> subprocess.CompletedProcess:
        self.calls.append((cmd, input))
        for prefix, result in self._scripted.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return result
        return self.default


def test_next_available_port_skips_reserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(satellite, "_port_in_use", lambda p: False)
    reg = {"a": {"local_fwd_port": 8766}, "b": {"local_fwd_port": 8767}}
    assert satellite.next_available_port(reg, start=8766) == 8768


def test_install_tunnel_writes_files_and_enables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _Runner()
    monkeypatch.setattr(satellite.paths, "baird_home", lambda: tmp_path / "baird")
    spec = satellite.TunnelSpec(
        ssh_host="workstation",
        local_fwd_port=8766,
        systemd_user_dir=tmp_path / "systemd",
        baird_config_dir=tmp_path / "bcfg",
    )
    satellite.install_tunnel(spec, run=runner)

    unit = (tmp_path / "systemd" / "baird-tunnel@.service").read_text()
    assert "ExecStart=" in unit
    assert "%i" in unit
    env = (tmp_path / "bcfg" / "tunnel-workstation.env").read_text()
    assert "LOCAL_FWD_PORT=8766" in env
    assert "SATELLITE_PORT=8765" in env
    cmds = [c[0][:3] for c in runner.calls]
    assert ["systemctl", "--user", "daemon-reload"] in cmds
    assert any("enable" in c for c in cmds[0] + cmds[-1])


def test_render_host_yaml_uses_hub_token() -> None:
    spec = satellite.EnrollSpec(
        ssh_host="workstation",
        host_id="workstation",
        hub_auth_token="tok123",
        use_hub_for_models=True,
    )
    out = satellite._render_host_yaml(spec, remote_home="/home/user")
    assert "host_id: workstation" in out
    assert 'hub_auth_token: "tok123"' in out
    assert "use_hub_for_models: true" in out
    assert "/home/user" in out


def test_enroll_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    monkeypatch.setattr(satellite, "_port_in_use", lambda _p: False)
    runner = _Runner()
    # Bootstrap echoes "OK /home/user".
    runner.script(
        ("ssh", "-o", "BatchMode=yes"),
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout="OK /home/user\n", stderr=""
        ),
    )

    spec = satellite.EnrollSpec(
        ssh_host="workstation", host_id="workstation", hub_auth_token="tok",
    )
    res = satellite.enroll(spec, run=runner)

    assert res.health_ok
    assert res.local_fwd_port == 8766
    assert res.remote_home == "/home/user"

    reg = satellite.load_registry()
    assert reg["workstation"]["ssh_host"] == "workstation"
    assert reg["workstation"]["local_fwd_port"] == 8766

    # The host.yaml-writing ssh call should have received the rendered yaml.
    yaml_write = next(
        (c for c in runner.calls if c[0][3:5] == ["workstation", "mkdir -p $HOME/.baird && cat > $HOME/.baird/host.yaml"]),
        None,
    )
    assert yaml_write is not None
    assert 'hub_auth_token: "tok"' in yaml_write[1]


def test_enroll_bootstrap_failure_reports_and_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAIRD_HOME", str(tmp_path))
    runner = _Runner()
    runner.script(
        ("ssh", "-o", "BatchMode=yes"),
        subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="ssh: connect failed"
        ),
    )
    spec = satellite.EnrollSpec(ssh_host="nope", host_id="nope", hub_auth_token="t")
    res = satellite.enroll(spec, run=runner)
    assert not res.health_ok
    assert "ssh: connect failed" in res.detail


def test_remove_tunnel_calls_disable_and_unlinks_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _Runner()
    spec = satellite.TunnelSpec(
        ssh_host="workstation",
        local_fwd_port=8766,
        systemd_user_dir=tmp_path / "s",
        baird_config_dir=tmp_path / "b",
    )
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "tunnel-workstation.env").write_text("LOCAL_FWD_PORT=8766\n")
    satellite.remove_tunnel(spec, run=runner)
    assert not (tmp_path / "b" / "tunnel-workstation.env").exists()
    assert ["systemctl", "--user", "disable", "--now"] == runner.calls[0][0][:4]
