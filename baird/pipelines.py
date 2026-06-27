"""Snakemake / Nextflow shell-out wrappers — Phase 4 design (#4).

We deliberately do NOT use the python libraries for either tool — the
versions move and the APIs change. Instead we shell out, capture the output
and the framework's own report, and post a child-action timeline back to the
hub.

Lifecycle of one wrapper call:

  1. Open a *parent* Action keyed by the framework name + the workflow file.
     This is what shows up in `baird status` / `baird registry actions`.
  2. (Optional) Pick up an `env:` block from the project YAML and prepend the
     activation prefix (Phase 3 #7 / 4b env.py).
  3. Run the command — inline if short, or detached inside a multiplexer
     session if `--detach` is requested.
  4. Parse the framework's report file (Snakemake JSON / Nextflow trace text)
     and write a one-paragraph summary onto the parent action.
  5. Post a `result` notification.

This module is sync + deterministic. The runner thread can call it; a CLI
command can call it directly. The subprocess.run call is dependency-injected
so tests don't need snakemake/nextflow installed.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory_client import HubClient
from .notifier import Notifier


# Pluggable runner: takes (argv, cwd) → (returncode, stdout, stderr).
RunnerFn = Callable[[list[str], str | None], tuple[int, str, str]]


def _default_runner(argv: list[str], cwd: str | None) -> tuple[int, str, str]:
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


# Streaming runner contract for `--live` mode. Yields (line_kind, line) pairs
# where line_kind is "out" or "err"; returns the final (returncode, full_stdout,
# full_stderr) when the process exits.
StreamingRunnerFn = Callable[
    [list[str], str | None, Callable[[str, str], None]],
    tuple[int, str, str],
]


def _default_streaming_runner(
    argv: list[str],
    cwd: str | None,
    on_line: Callable[[str, str], None],
) -> tuple[int, str, str]:
    """Run a subprocess line-by-line. `on_line(kind, line)` is invoked for
    every stdout (kind=\"out\") or stderr (kind=\"err\") line."""
    import threading

    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    out_buf: list[str] = []
    err_buf: list[str] = []

    def _pump(stream, buf, kind):
        for line in stream:
            buf.append(line)
            try:
                on_line(kind, line.rstrip("\n"))
            except Exception:
                pass
        stream.close()

    t1 = threading.Thread(target=_pump, args=(proc.stdout, out_buf, "out"), daemon=True)
    t2 = threading.Thread(target=_pump, args=(proc.stderr, err_buf, "err"), daemon=True)
    t1.start(); t2.start()
    proc.wait()
    t1.join(); t2.join()
    return proc.returncode, "".join(out_buf), "".join(err_buf)


# Pattern Snakemake prints between jobs: "5 of 27 steps (18%) done"
_PROGRESS_LINE = re.compile(r"^(\d+)\s+of\s+(\d+)\s+steps?\s*\((\d+)%\)\s*done\.?\s*$")


@dataclass
class PipelineResult:
    tool: str
    workflow: str
    action_id: str
    exit_code: int
    runtime_s: float
    rules_total: int | None = None
    rules_completed: int | None = None
    summary: str = ""
    raw_stdout: str = ""
    raw_stderr: str = ""
    child_summaries: list[str] = field(default_factory=list)


# ---- Snakemake ---------------------------------------------------------


def snakemake_run(
    *,
    workflow: Path,
    extra_args: list[str] | None = None,
    cwd: Path | None = None,
    hub: HubClient,
    notifier: Notifier | None = None,
    project_id: str | None = None,
    host_id: str | None = None,
    activation_prefix: str = "",
    runner: RunnerFn = _default_runner,
    streaming_runner: StreamingRunnerFn = _default_streaming_runner,
    report_path: Path | None = None,
    live: bool = False,
    on_progress: Callable[[dict], None] | None = None,
) -> PipelineResult:
    """Run `snakemake -s <workflow>` and post a result back to the hub.

    `live=True` switches to a streaming runner. Each "X of Y steps (Z%) done"
    line emits a progress update — `on_progress(dict)` if supplied, otherwise
    a `logged` inbox row via the notifier (rate-limited to every 10%).
    """
    cwd = (cwd or workflow.parent).resolve()
    args = ["snakemake", "-s", str(workflow), *(extra_args or [])]
    cmd_for_log = activation_prefix + " ".join(args) if activation_prefix else " ".join(args)
    started = time.monotonic()

    with hub.start_action(
        project_id=project_id,
        tool_name="snakemake",
        command=cmd_for_log,
        host=host_id,
    ) as action:
        if live:
            last_pct = [-1]

            def _on_line(kind: str, line: str) -> None:
                m = _PROGRESS_LINE.match(line.strip())
                if not m:
                    return
                done, total, pct = int(m.group(1)), int(m.group(2)), int(m.group(3))
                progress = {
                    "done": done, "total": total, "percent": pct,
                    "action_id": action.id, "workflow": str(workflow),
                }
                if on_progress is not None:
                    on_progress(progress)
                # Throttle inbox spam: only at 10% boundaries.
                bucket = pct // 10
                if notifier is not None and bucket != last_pct[0]:
                    last_pct[0] = bucket
                    notifier.notify(
                        kind="logged",
                        title=f"snakemake {workflow.name} {pct}%",
                        body=f"{done} of {total} steps done",
                        project_id=project_id,
                        action_id=action.id,
                    )

            code, out, err = streaming_runner(args, str(cwd), _on_line)
        else:
            code, out, err = runner(args, str(cwd))
        runtime = time.monotonic() - started

        info = _parse_snakemake(report_path, out, err)
        summary = _render_pipeline_summary(
            tool="snakemake", exit_code=code, runtime_s=runtime, info=info
        )
        action.set_summary(summary)
        action.set_exit_code(code)

    result = PipelineResult(
        tool="snakemake",
        workflow=str(workflow),
        action_id=action.id,
        exit_code=code,
        runtime_s=runtime,
        rules_total=info.get("rules_total"),
        rules_completed=info.get("rules_completed"),
        summary=summary,
        raw_stdout=out,
        raw_stderr=err,
        child_summaries=info.get("rule_lines", []),
    )

    if notifier is not None:
        notifier.notify(
            kind="result" if code == 0 else "failure",
            title=f"snakemake {workflow.name} {'done' if code == 0 else 'failed'}",
            body=summary,
            project_id=project_id,
            action_id=action.id,
        )
    return result


def _parse_snakemake(report_path: Path | None, stdout: str, stderr: str) -> dict[str, Any]:
    """Best-effort: prefer a JSON report file if given; else parse the
    standard "X of Y steps (Z%) done" line that Snakemake prints."""
    info: dict[str, Any] = {}

    if report_path and report_path.exists():
        try:
            data = json.loads(report_path.read_text())
            jobs = data.get("jobs") or []
            info["rules_total"] = len(jobs)
            info["rules_completed"] = sum(1 for j in jobs if j.get("status") == "completed")
            info["rule_lines"] = [
                f"{j.get('rule', '?')} → {j.get('status', '?')}" for j in jobs[:20]
            ]
        except (json.JSONDecodeError, OSError):
            pass

    combined = (stdout or "") + "\n" + (stderr or "")
    m = re.search(r"(\d+)\s+of\s+(\d+)\s+steps", combined)
    if m and "rules_completed" not in info:
        info["rules_completed"] = int(m.group(1))
        info["rules_total"] = int(m.group(2))

    return info


# ---- Nextflow ----------------------------------------------------------


def nextflow_run(
    *,
    workflow: Path,
    extra_args: list[str] | None = None,
    cwd: Path | None = None,
    hub: HubClient,
    notifier: Notifier | None = None,
    project_id: str | None = None,
    host_id: str | None = None,
    activation_prefix: str = "",
    runner: RunnerFn = _default_runner,
    trace_path: Path | None = None,
) -> PipelineResult:
    """Run `nextflow run <workflow>` and post a result back."""
    cwd = (cwd or workflow.parent).resolve()
    args = ["nextflow", "run", str(workflow), *(extra_args or [])]
    cmd_for_log = activation_prefix + " ".join(args) if activation_prefix else " ".join(args)
    started = time.monotonic()

    with hub.start_action(
        project_id=project_id,
        tool_name="nextflow",
        command=cmd_for_log,
        host=host_id,
    ) as action:
        code, out, err = runner(args, str(cwd))
        runtime = time.monotonic() - started

        info = _parse_nextflow(trace_path, out)
        summary = _render_pipeline_summary(
            tool="nextflow", exit_code=code, runtime_s=runtime, info=info
        )
        action.set_summary(summary)
        action.set_exit_code(code)

    result = PipelineResult(
        tool="nextflow",
        workflow=str(workflow),
        action_id=action.id,
        exit_code=code,
        runtime_s=runtime,
        rules_total=info.get("rules_total"),
        rules_completed=info.get("rules_completed"),
        summary=summary,
        raw_stdout=out,
        raw_stderr=err,
        child_summaries=info.get("rule_lines", []),
    )

    if notifier is not None:
        notifier.notify(
            kind="result" if code == 0 else "failure",
            title=f"nextflow {workflow.name} {'done' if code == 0 else 'failed'}",
            body=summary,
            project_id=project_id,
            action_id=action.id,
        )
    return result


def _parse_nextflow(trace_path: Path | None, stdout: str) -> dict[str, Any]:
    """Best-effort: parse a Nextflow trace.txt (TSV with status column) if given."""
    info: dict[str, Any] = {}

    if trace_path and trace_path.exists():
        try:
            lines = trace_path.read_text().splitlines()
            if lines:
                header = [c.strip() for c in lines[0].split("\t")]
                status_idx = header.index("status") if "status" in header else None
                process_idx = header.index("process") if "process" in header else None
                rows = [r.split("\t") for r in lines[1:] if r.strip()]
                info["rules_total"] = len(rows)
                if status_idx is not None:
                    info["rules_completed"] = sum(
                        1 for r in rows if r[status_idx].strip() in {"COMPLETED", "CACHED"}
                    )
                if process_idx is not None and status_idx is not None:
                    info["rule_lines"] = [
                        f"{r[process_idx]} → {r[status_idx]}" for r in rows[:20]
                    ]
        except (OSError, ValueError, IndexError):
            pass

    # Heuristic from stdout: "executor >  local" then per-process lines.
    m = re.findall(r"\[\s*[0-9a-f/]+\s*\]\s+process\s+>\s+([^\s]+)", stdout or "")
    if m and "rules_total" not in info:
        info["rules_total"] = len(set(m))

    return info


# ---- Shared rendering --------------------------------------------------


def _render_pipeline_summary(*, tool: str, exit_code: int, runtime_s: float, info: dict) -> str:
    bits = [f"{tool} exit={exit_code} runtime={runtime_s:.1f}s"]
    if info.get("rules_total") is not None:
        done = info.get("rules_completed", "?")
        bits.append(f"rules: {done}/{info['rules_total']} completed")
    if info.get("rule_lines"):
        bits.append("first rules:")
        bits.extend(f"  - {line}" for line in info["rule_lines"][:5])
    return "\n".join(bits)
