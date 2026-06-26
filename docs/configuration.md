# Configuration

Four files, two env vars.

| File | Owner | Purpose |
|---|---|---|
| `~/.baird/host.yaml` | every machine | volume map, watch roots, hub URL, auth token, multiplexer choice |
| `~/.baird/config.yaml` | hub only | hub listen address, DB paths, daily budget ceilings |
| `<repo>/.baird/project.yaml` | per project | identity, context, goals, decisions, rules, env block |
| `~/.baird/tasks/<id>.yaml` | hub only | one file per scheduled/background task |

Plus environment variables:

| Var | Used by | Notes |
|---|---|---|
| `BAIRD_HOME` | everything | state directory. Default `~/.baird`. Set to run two installs side-by-side (dev vs. prod). |
| `OPENROUTER_API_KEY` | `baird code`, `baird task run`, `baird research`, `baird improve`, orchestrator | required to call the model |
| `TELEGRAM_BOT_TOKEN` | orchestrator (Notifier) | optional; without it, inbox-only |
| `TELEGRAM_CHAT_ID` | orchestrator (Notifier) | required if `TELEGRAM_BOT_TOKEN` is set |
| `TAVILY_API_KEY` | `baird research` (default backend) | optional; without it, research falls back gracefully |

### Running two installs side-by-side

Set `BAIRD_HOME` to redirect every state file (`host.yaml`, `config.yaml`, `tasks/`,
`registry.sqlite`, `memory.sqlite`) to a different directory:

```bash
# Prod (your real harness): default ~/.baird
alias baird='BAIRD_HOME=$HOME/.baird ~/code/BAIRD-prod/.venv/bin/baird'

# Dev (for breaking things): separate state under ~/.baird-dev
alias baird-dev='BAIRD_HOME=$HOME/.baird-dev ~/code/BAIRD-dev/.venv/bin/baird'
```

Use a different `listen:` port in the dev `config.yaml` so the two hubs can run
at the same time. Your project memory lives in the SQLite files under
`$BAIRD_HOME`, so the two installs share nothing by default.

## `~/.baird/host.yaml`

Loaded by `baird daemon`, by every CLI command (to know which hub to talk to), and by the executor.

```yaml
host_id: surface                       # any short string; appears in action rows
hub_url: http://127.0.0.1:8000         # where the FastAPI hub lives
session_multiplexer: auto              # auto | tmux | screen | none
auth_token: null                       # bearer token the executor requires; null = deny remote calls
executor_listen: null                  # "0.0.0.0:8765" to expose the executor; null disables it

volumes:                               # one entry per storage volume on this machine
  - id: surface:/home                  # `host:path` style; used as storage_volume in the registry
    mount: /home/ethan                 # absolute path on this machine
    shared: false                      # true for cluster-shared filesystems
  - id: cluster:/work
    mount: /work
    shared: true

watch:
  roots:                               # what the watchdog scans (recursively)
    - /home/ethan/projects
    - /work/experiments
  deny:                                # gitignore-style patterns to skip
    - "**/.git/**"
    - "**/__pycache__/**"
    - "**/.snakemake/**"
    - "**/.nextflow/**"
    - "**/conda-envs/**"
    - "**/.ipynb_checkpoints/**"
    - "**/*.swp"
```

### Volume modelling

A volume is `(host, path-on-host)`, not just a host. Each watched file is identified by `(storage_volume, relative_path)`. The longest-prefix-wins rule means nested volumes (`cluster:/work` and `cluster:/work/scratch`) resolve to the most specific match.

### Multiplexer choice

- `auto` — prefer tmux, fall back to screen, fall back to noop (subprocess-only)
- `tmux` / `screen` — force one; error if missing
- `none` — never use a multiplexer (use this on machines where attach isn't useful, like a remote CI runner)

### Executor binding

Leave `executor_listen: null` on machines you never call from elsewhere (your hub, when you're the only user). Set it (e.g. `0.0.0.0:8765` bound to a Tailscale interface) on satellites the hub will drive. **Always** set `auth_token` when exposing the executor.

## `~/.baird/config.yaml`

Hub-side only. Loaded by `baird hub serve` and the orchestrator.

```yaml
listen: 127.0.0.1:8000                 # FastAPI bind
registry_db: ~/.baird/registry.sqlite  # path; created on first run
memory_db: ~/.baird/memory.sqlite

daily_total_usd: 5.0                   # global ceiling per 24h
daily_per_task_default_usd: 0.5        # fallback when a task has no max_cost_usd
```

Defaults are reasonable for a single user; you rarely need to edit this.

## `<repo>/.baird/project.yaml`

Per-project, committed to the repo so it travels with the code. `baird project init <id>` writes a starter version.

```yaml
id: scrna-2026
name: scRNA pipeline 2026
github: ethanlewisbaird/scrna-2026
context: |
  Integration of 3 publicly-available scRNA-seq datasets to build a unified atlas.

checkout_hosts:                        # which machines currently have this project cloned
  - host_id: surface
    path: /home/ethan/projects/scrna-2026
    branch: main

goals:
  - id: g1
    text: Reproduce Smith et al. integration benchmark
    status: active                     # active | done | abandoned

state:
  phase: qc

data_aliases:                          # short names for full volume paths
  - name: raw
    volume: cluster:/work
    path: scrna-2026/raw

rules:                                 # enforceable best-practices
  - id: seeds-set
    description: Random-seed CLI commands must pass a seed
    applies_to: [python, R]
    enforce: pre_execution             # pre_execution | post_execution | on_review
    check: seeds_set                   # built-in checker name
    severity: warn                     # warn | block
    params:
      triggers: [scanpy, sklearn, umap, leiden]
  - id: ai-friendly-outputs
    description: PDFs/PNGs need a CSV/JSON sibling
    applies_to: ["**/*.pdf", "**/*.png"]
    enforce: post_execution
    check: ai_friendly_outputs
    severity: warn

env:                                   # optional: pin an environment
  conda: bio-py311
  # or: docker: biocontainers/samtools
  # or: singularity: /sif/tool.sif
  #     bind_paths: [/data]

permissions:                           # optional: per-project overrides for the safe/destructive gate
  - command_regex: "^./run_pipeline\\.sh"
    tier: project
    reason: vetted pipeline runner
```

### Built-in rule checkers

| `check:` | What it does | Suggested `enforce:` |
|---|---|---|
| `seeds_set` | If `params.triggers` (default scanpy/sklearn/umap/leiden/...) appear in the command, require an explicit seed | `pre_execution` |
| `env_pinned` | Project root must contain an env spec (`environment.yml` / `Dockerfile` / `*.sif` / `renv.lock` / `pyproject.toml`) | `on_review` |
| `readme_present` | Project root has a `README.*` | `on_review` |
| `ai_friendly_outputs` | Each plot output has a sibling CSV/JSON/Parquet/TSV with the same stem | `post_execution` |

Rules with `severity: block` abort the action; `warn` just records a warning.

## `~/.baird/tasks/<id>.yaml`

One file per scheduled task. `baird task add <id>` writes a starter.

```yaml
id: morning-poke
description: Daily project pulse
enabled: true

trigger:
  type: cron                           # cron | interval | watch | reactive
  cron: "0 9 * * *"
  one_shot: false                      # cron tasks that disable themselves after one fire

runnable:
  prompt: "Summarise what changed in the last 24h and flag anything that looks off."
  model: anthropic/claude-3-haiku
  system: null                         # optional system prompt
  project_id: scrna-2026               # optional — gates project-context loading
  context_sources: [repo, decisions, rules]
  max_tokens: 1024
  temperature: 0.2

budget:
  max_runtime_s: 120
  max_cost_usd: 0.10
  max_tokens: null
  max_actions: null

concurrency_group: null                # tasks in same group serialize
on_failure: {}                         # reserved
```

### Trigger types

```yaml
# Every 5 minutes
trigger:
  type: interval
  interval_seconds: 300

# Standard 5-field cron
trigger:
  type: cron
  cron: "*/15 * * * *"
  one_shot: false

# On filesystem change under a path
trigger:
  type: watch
  path: ~/projects/scrna-2026/results
  events: [created, modified]          # created | modified | moved | deleted

# On an in-process event
trigger:
  type: reactive
  event: action.failed_3x              # any event name; emit with EventBus.publish()
```

Watch firings are debounced (default 2s per task) so editor save bursts don't multi-fire.

### Concurrency groups

Tasks declaring the same `concurrency_group` will not run concurrently:

```yaml
concurrency_group: "global-mutator"
```

The global thread pool size is 3 by default (`baird orchestrator serve --max-workers N`).
