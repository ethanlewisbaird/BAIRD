"""Tests for the Snakemake/Nextflow wrappers (with injected fake runners)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baird.memory_client import HubClient
from baird.notifier import FakeTelegramTransport, Notifier, TelegramConfig
from baird.pipelines import nextflow_run, snakemake_run


class _Hub(HubClient):
    def __init__(self, client: TestClient) -> None:
        self._client = client


# ---- Snakemake ---------------------------------------------------------


def test_snakemake_success_parses_progress_line(client: TestClient, tmp_path: Path) -> None:
    wf = tmp_path / "Snakefile"
    wf.write_text("rule all: input: []\n")

    def runner(argv, cwd):
        return 0, "Building DAG of jobs...\n4 of 4 steps (100%) done\n", ""

    hub = _Hub(client)
    res = snakemake_run(workflow=wf, hub=hub, runner=runner)
    assert res.exit_code == 0
    assert res.rules_total == 4
    assert res.rules_completed == 4
    assert "snakemake exit=0" in res.summary
    # Action row should be finished with the summary.
    action = hub.get_action(res.action_id)
    assert action["exit_code"] == 0
    assert "snakemake" in (action["summary"] or "")


def test_snakemake_failure_notifies_failure(client: TestClient, tmp_path: Path) -> None:
    wf = tmp_path / "Snakefile"
    wf.write_text("rule all: input: []\n")

    def runner(argv, cwd):
        return 1, "", "MissingInputException: foo"

    hub = _Hub(client)
    tg = FakeTelegramTransport()
    notifier = Notifier(hub=hub, telegram=TelegramConfig(bot_token="t", chat_id="1"), transport=tg)
    res = snakemake_run(workflow=wf, hub=hub, notifier=notifier, runner=runner)
    assert res.exit_code == 1
    assert any("failed" in text for _, text in tg.sent)


def test_snakemake_json_report_is_preferred(client: TestClient, tmp_path: Path) -> None:
    wf = tmp_path / "Snakefile"
    wf.write_text("x")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"jobs": [
        {"rule": "a", "status": "completed"},
        {"rule": "b", "status": "completed"},
        {"rule": "c", "status": "failed"},
    ]}))

    def runner(argv, cwd):
        return 0, "irrelevant stdout", ""

    hub = _Hub(client)
    res = snakemake_run(workflow=wf, hub=hub, runner=runner, report_path=report)
    assert res.rules_total == 3
    assert res.rules_completed == 2


# ---- Nextflow ----------------------------------------------------------


def test_nextflow_parses_trace_file(client: TestClient, tmp_path: Path) -> None:
    wf = tmp_path / "main.nf"
    wf.write_text("workflow {}\n")
    trace = tmp_path / "trace.txt"
    trace.write_text(
        "task_id\tprocess\tstatus\n"
        "1\tFOO\tCOMPLETED\n"
        "2\tBAR\tCACHED\n"
        "3\tBAZ\tFAILED\n"
    )

    def runner(argv, cwd):
        return 0, "executor >  local (3)\n", ""

    hub = _Hub(client)
    res = nextflow_run(workflow=wf, hub=hub, runner=runner, trace_path=trace)
    assert res.rules_total == 3
    assert res.rules_completed == 2


def test_nextflow_failure_returns_nonzero(client: TestClient, tmp_path: Path) -> None:
    wf = tmp_path / "main.nf"
    wf.write_text("x")

    def runner(argv, cwd):
        return 2, "", "ERROR"

    hub = _Hub(client)
    res = nextflow_run(workflow=wf, hub=hub, runner=runner)
    assert res.exit_code == 2


def test_pipeline_activation_prefix_recorded(client: TestClient, tmp_path: Path) -> None:
    wf = tmp_path / "Snakefile"
    wf.write_text("x")

    def runner(argv, cwd):
        return 0, "", ""

    hub = _Hub(client)
    res = snakemake_run(
        workflow=wf,
        hub=hub,
        runner=runner,
        activation_prefix='conda activate myenv && ',
    )
    action = hub.get_action(res.action_id)
    assert "conda activate myenv" in (action["command"] or "")
