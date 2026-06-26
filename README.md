# BAIRD

**Bioinformatics AI Research Daemon** — a personal AI harness that unifies interactive coding and autonomous background work across a multi-machine setup, with provenance tracked end-to-end.

One always-on **hub** runs on the Linux server; a lightweight **daemon** runs on each satellite (workstation, HPC cluster, laptop). Both modes share one memory: the background agent knows what you were coding, the coding agent has access to project goals and prior decisions, and every file or action either of them produces is logged.

## Status

End-to-end usable. Phases 1–5, the substrate side of 4b/5b, and the production-readiness pass are all shipped:

| Slice | What works |
|---|---|
| 1. Storage & provenance | Hub registry (FastAPI + SQLite), watchdog daemon with fast fingerprint + lazy sha256 backfill, dedup-on-write, soft-delete |
| 2. Shared memory core | `.baird/project.yaml` schema + rules engine, decisions, actions, sessions, messages, notifications, `recall` (SQL-backed for now) |
| 3. Coding-mode substrate | Three-tier safe/destructive classifier, satellite executor (`read_file`/`write_file`/`run_command`/`apply_diff`), repo context loader, diff apply + `undo` |
| 4. Background-agent mode | OpenRouter client, task YAML, threaded scheduler (cron+interval+watch+reactive), per-task + global daily budgets, Telegram notifier, persistent per-task conversation threads |
| 4b. Features | Multi-turn REPL with diff approval + `/model` picker, Snakemake/Nextflow wrappers, self-improvement loop, research loop, tmux/screen session abstraction, env activation prefix |
| 5. Observability | `status` dashboard (one-shot + `--watch`), `logs`/`ps`/`registry actions`/`task history`, mode auto-detection on bare `baird` |
| Multi-machine | Bearer-token hub auth, `/v1/proxy/chat/completions` (key lives only on the hub), one-command `baird satellite enroll`, persistent SSH tunnels via systemd-user, single unified cost/token ledger |
| Operations | `<baird_home>/secrets.env` for credentials, auto-add missing columns on engine startup, `baird up`/`stop` hub supervisor, `$BAIRD_HOME` for dev/prod split |

What's still deferred is called out in [docs/design.md](docs/design.md).

## Quick start (single machine)

```bash
# 1. install
git clone git@github.com:ethanlewisbaird/BAIRD.git && cd BAIRD
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. minimal host config
mkdir -p ~/.baird
cat > ~/.baird/host.yaml <<EOF
host_id: $(hostname)
hub_url: http://127.0.0.1:8000
volumes:
  - id: $(hostname):/home
    mount: $HOME
    shared: false
watch:
  roots: [ $HOME/baird-sandbox ]
  deny:  [ "**/.git/**" ]
EOF
mkdir -p ~/baird-sandbox

# 3. credentials (no .bashrc edits needed)
cat > ~/.baird/secrets.env <<EOF
OPENROUTER_API_KEY=sk-or-...
EOF
chmod 600 ~/.baird/secrets.env

# 4. enrol a project + open the coding REPL
cd ~/baird-sandbox && git init && git commit --allow-empty -m init
baird project init sandbox --name "Sandbox"
baird code              # hub auto-starts; multi-turn REPL with diff approval

# 5. see what's going on
baird status            # one-shot dashboard
baird status --watch    # live
```

## Adding satellite machines

From the hub (one command per satellite):

```bash
baird satellite enroll <ssh-host>       # SSHes out, installs BAIRD, sets up
                                        # systemd-user tunnel, verifies round-trip
baird satellite list                    # show enrolled satellites + tunnel status
```

The satellite gets the hub's auth token written into its `host.yaml` automatically and routes all model calls through `/v1/proxy/chat/completions` — the OpenRouter key never leaves the hub.

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
pytest                  # 283 tests
ruff check baird tests
```

## License

MIT. See `LICENSE`.
