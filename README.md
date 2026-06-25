# BAIRD

**Bioinformatics AI Research Daemon** — a personal AI harness that unifies interactive coding and autonomous background work across a multi-machine setup, with provenance tracked end-to-end.

One always-on **hub** runs on the Linux server; a lightweight **daemon** runs on each satellite (workstation, HPC cluster, laptop). Both modes share one memory: the background agent knows what you were coding, the coding agent has access to project goals and prior decisions, and every file or action either of them produces is logged.

## Status

End-to-end usable. Phases 1–5 plus the substrate side of 4b/5b plus the major Phase 4 features are all shipped:

| Slice | What works |
|---|---|
| 1. Storage & provenance | Hub registry (FastAPI + SQLite), watchdog daemon with fast fingerprint + lazy sha256 backfill, dedup-on-write, soft-delete |
| 2. Shared memory core | `.baird/project.yaml` schema + rules engine, decisions, actions, sessions, messages, notifications, `recall` (SQL-backed for now) |
| 3. Coding-mode substrate | Three-tier safe/destructive classifier, satellite executor (`read_file`/`write_file`/`run_command`/`apply_diff`), repo context loader, diff apply + `undo` |
| 4. Background-agent mode | OpenRouter client, task YAML, threaded scheduler (cron+interval+watch+reactive), per-task + global daily budgets, Telegram notifier, persistent per-task conversation threads |
| 4b. Features | Multi-turn REPL with diff approval, Snakemake/Nextflow wrappers, self-improvement loop, research loop, tmux/screen session abstraction, env activation prefix |
| 5. Observability | `status` dashboard (one-shot + `--watch`), `logs`/`ps`/`registry actions`/`task history`, mode auto-detection on bare `baird` |

What's still deferred is called out in [docs/design.md](docs/design.md).

## Quick start

```bash
# 1. install
git clone git@github.com:ethanlewisbaird/BAIRD.git && cd BAIRD
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. config (hub-side defaults are fine for one machine)
mkdir -p ~/.baird
cat > ~/.baird/host.yaml <<EOF
host_id: $(hostname)
hub_url: http://127.0.0.1:8000
volumes:
  - id: $(hostname):/home
    mount: $HOME
    shared: false
watch:
  roots:
    - $HOME/baird-sandbox
  deny:
    - "**/.git/**"
EOF
mkdir -p ~/baird-sandbox

# 3. run hub + daemon (one terminal each)
baird hub serve         # terminal A
baird daemon            # terminal B

# 4. enrol a project + open the coding REPL
cd ~/baird-sandbox && git init
baird project init sandbox --name "Sandbox"
export OPENROUTER_API_KEY=sk-or-...
baird code              # multi-turn REPL with diff approval

# 5. see what's going on
baird status            # one-shot dashboard
baird status --watch    # live
```

Full step-by-step: [docs/quickstart.md](docs/quickstart.md).

## Documentation

- **[Quickstart](docs/quickstart.md)** — first-run walkthrough
- **[Architecture](docs/architecture.md)** — what runs where, data model, security model
- **[Configuration](docs/configuration.md)** — `host.yaml`, hub config, project YAML, task YAML, env vars
- **[Commands](docs/commands.md)** — CLI reference
- **[Workflows](docs/workflows.md)** — common recipes (interactive coding, scheduled tasks, pipelines, research, self-improvement)
- **[Design](docs/design.md)** — design decisions, deferred items

## Development

```bash
pip install -e ".[dev]"
pytest                  # 253 tests
ruff check baird tests
```

## License

MIT. See `LICENSE`.
