# BAIRD

**Bioinformatics AI Research Daemon** — a personal AI harness that unifies interactive coding and autonomous background work across a multi-machine bioinformatics setup, with provenance tracked end-to-end.

## What it is

BAIRD runs as a hub on an always-on Linux server with lightweight daemons on each satellite machine (workstation, HPC cluster, laptop). It replaces ad-hoc combinations of Claude Code and Hermes-style agents with one tool that:

- **Tracks every file and action** the AI (or you) produces, with full lineage — what command, what env, what inputs, on what host.
- **Coordinates work across machines** without bulk-syncing data — bulk files stay where they're produced, only metadata lives centrally.
- **Runs both modes from one place**: interactive coding sessions (diff-approval loop, conda-aware execution, tmux-persisted long jobs) and autonomous background tasks (cron, file-watch, reactive triggers).
- **Shares memory** between modes — the background agent knows what you were coding; the coding agent has access to project goals and prior decisions.

## Status

**Scaffolding only.** Not yet runnable as a service. See `docs/design.md` for the design that this scaffold will be filled out to implement, in five phases:

1. Storage & provenance foundation
2. Shared memory core
3. Interactive coding mode
4. Background agent mode
5. Orchestration & UX polish

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The `baird` CLI installs as a console entry point (`baird --help`).

## Layout

```
baird/
├── cli.py             # Typer-based CLI entry
├── config.py          # config loading
├── fingerprint.py     # fast file-identity fingerprint (size, mtime, head/tail sha256)
├── db.py              # SQLAlchemy models (registry + memory)
├── hub.py             # FastAPI app — registry & memory service (runs on hub)
├── daemon.py          # watchdog + executor daemon (runs on each satellite)
└── memory_client.py   # HTTP client library for the hub's REST API
```

## License

MIT. See `LICENSE`.
